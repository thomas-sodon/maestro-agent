"""Remediation preflight, stuck-PR escalation, merge-block probe and run-time
stand-down (``maestro/loop/pr_remediation.py``); split out of
``tests/test_agent_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import gate_capacity
from maestro.consciousness import PendingPRRequest
from maestro.loop.pr_remediation import PreflightRestorationError
from maestro.pr_facts import MergeTruth
from maestro.vcs import PrMergeState
from tests.conftest import MockQueryResult

# ──────────────────────────────────────────────────────────────────────
# Pre-remediation quality gate (preflight PR check)
# ──────────────────────────────────────────────────────────────────────


def _preflight_git_stdout(cmd: object, head_sha: str) -> str:
    """Stdout for the git reads ``preflight`` issues, given a branch HEAD sha.

    ``preflight``'s ``finally`` block proves restoration rather than hoping for
    it (#3715): after the reset and the return to ``main`` it reads
    ``git rev-parse --abbrev-ref HEAD`` and ``git status --porcelain``. This
    helper answers the restored happy path — on ``main``, clean tree — so every
    preflight test's command-aware subprocess mock keeps a single source of
    truth for those readings.
    """
    if cmd == ["git", "rev-parse", "HEAD"]:
        return f"{head_sha}\n"
    if isinstance(cmd, list) and cmd[:3] == ["git", "rev-parse", "--verify"]:
        return f"{head_sha}\n"
    if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
        return "main\n"
    return ""


@pytest.mark.asyncio
async def test_preflight_remediation_skips_when_gates_pass(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the probe-merge succeeds and quality gates all pass on the
    *merged* PR tree, the remediation task is recorded with a
    ``[preflight]`` finding and the full LLM flow (``agent.run``) is
    never invoked. The probe-merge commit is reset away (never pushed)."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #1: fix lint\n\n## Problem\n"
                "After merging origin/main into PR #1, quality gates "
                "failed: ruff exit 1. Fix the underlying breakage."
            ),
            pr_branch="maestro/evolve/p1",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    captured: list[object] = []

    def _all_pass(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        # ``git rev-parse HEAD`` must report a sha so the probe-merge
        # commit can be reset away in the ``finally`` block.
        stdout = _preflight_git_stdout(cmd, "abc123")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_all_pass):
        await agent._run_task_phase()

    # LLM flow must NOT have been invoked.
    agent.run.assert_not_awaited()

    # The probe-merge ran against origin/main, and its commit was reset
    # away before returning to main — preflight never pushes.
    assert ["git", "merge", "--no-ff", "--no-edit", "origin/main"] in captured
    assert ["git", "reset", "--hard", "abc123"] in captured
    assert ["git", "checkout", "main"] in captured

    # A task record with the preflight marker must have been persisted.
    tasks = agent.memory.get_recent_tasks(5)
    assert len(tasks) == 1
    rec = tasks[0]
    assert rec.outcome == "success"
    assert "[preflight]" in (rec.key_findings or "")
    assert "maestro/evolve/p1" in (rec.key_findings or "")

    # The PR request was drained from the queue.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []


@pytest.mark.asyncio
async def test_preflight_gate_commands_honour_work_target_atoms(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A ``[work]``-overridden gate target retargets the preflight gates (proposal 107).

    The preflight builds its ruff/mypy/pytest gate list from
    ``config.work_target()`` rather than hardcoded literals, so overriding the
    ``[work]`` gate atoms swaps the source/tests paths (and ``--cov``) in the
    commands run on the merged PR tree — by config alone.
    """
    agent = make_agent()
    agent.config.work_gate_source_path = "widget/"
    agent.config.work_gate_tests_path = "widget_tests/"
    agent.config.work_gate_lint_extra_paths = ["widget_scripts/"]
    entry = PendingPRRequest(
        directive="Remediate PR #1: fix lint",
        pr_branch="maestro/evolve/p1",
    )

    captured: list[object] = []

    def _all_pass(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        stdout = _preflight_git_stdout(cmd, "abc123")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.loop.pr_remediation.subprocess.run", side_effect=_all_pass):
        result = await agent._pr_remediation.preflight(entry)

    assert result is True
    assert ["ruff", "check", "widget/", "widget_tests/", "widget_scripts/"] in captured
    assert ["mypy", "widget/", "widget_scripts/"] in captured
    pytest_cmd = next(
        c for c in captured if isinstance(c, list) and c[:1] == ["pytest"]
    )
    assert "widget_tests/" in pytest_cmd
    assert "widget_scripts/" not in pytest_cmd
    assert "--cov=widget" in pytest_cmd
    assert "--cov=maestro" not in pytest_cmd


@pytest.mark.asyncio
async def test_preflight_gate_commands_carry_lease_width_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag-on, the preflight pytest gate carries the granted ``-n``/``--dist loadfile`` width.

    The width comes from the shared ``lease_for_gate_run`` seam via
    ``worker_args``; the whole ruff -> mypy -> pytest sequence runs inside one held
    slot (released before the ``finally`` probe-merge reset). Flag off the commands
    stay byte-identical (``test_preflight_gate_commands_honour_work_target_atoms``);
    this is the flag-on sibling (proposal 126).
    """
    agent = make_agent()
    agent.config.gate_lease_enabled = True
    agent.config.gate_lease_cgroup_reserve_enabled = True
    agent.config.gate_lease_admission_cap_enabled = True
    entry = PendingPRRequest(
        directive="Remediate PR #1: fix lint",
        pr_branch="maestro/evolve/p1",
    )

    captured_slot_dir: list[object] = []
    captured_reserve: list[bool] = []
    captured_cap: list[object] = []
    captured_source: list[object] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, runs_path: object = None, cgroup_reserve: bool = False,
        source: object = "in_process", **kwargs: object,
    ) -> Iterator[gate_capacity.GateRun]:
        captured_slot_dir.append(slot_dir)
        captured_reserve.append(cgroup_reserve)
        captured_cap.append(kwargs.get("admission_cap"))
        captured_source.append(source)
        yield gate_capacity.GateRun(
            lease=gate_capacity.GateLease(workers=4, slots=8),
        )

    captured: list[object] = []

    def _all_pass(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        stdout = _preflight_git_stdout(cmd, "abc123")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch(
        "maestro.gate_capacity.lease_for_gate_run", fake_lease,
    ), patch(
        "maestro.loop.pr_remediation.subprocess.run", side_effect=_all_pass,
    ):
        result = await agent._pr_remediation.preflight(entry)

    assert result is True
    # The seam is keyed off the derived slot-dir property (flag on -> the slot dir).
    assert captured_slot_dir == [agent.config.gate_lease_slot_dir]
    # ...and the bound-aware reserve opt-in reaches the seam's worker plan.
    assert captured_reserve == [True]
    assert captured_cap == [True]
    # Preflight spends the flat selfcheck constant, never a lease-granted
    # deadline, so it names its own source and its rows stay out of
    # ``gate_capacity.derive_deadline``'s input (proposal 134).
    assert captured_source == ["preflight"]
    pytest_cmd = next(
        c for c in captured if isinstance(c, list) and c[:1] == ["pytest"]
    )
    assert pytest_cmd[-4:] == ["-n", "4", "--dist", "loadfile"]


@pytest.mark.asyncio
async def test_preflight_records_gate_run_row_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag-on, the preflight gate loop records one telemetry row via the real seam.

    Only the host readers are mocked, so the row lands via the actual recording
    path. All gates pass on the merged tree, so the recorded outcome is CLEAN
    and the admission is 'slot' (proposal 126).
    """
    agent = make_agent()
    agent.config.gate_lease_enabled = True
    entry = PendingPRRequest(
        directive="Remediate PR #1: fix lint",
        pr_branch="maestro/evolve/p1",
    )

    @contextmanager
    def fake_acquire(lock_dir: object, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "slot"

    def _all_pass(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        stdout = _preflight_git_stdout(cmd, "abc123")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch(
        "maestro.gate_capacity.measure_host_capacity",
        return_value=gate_capacity.HostCapacity(cores=8, available_mib=64000),
    ), patch(
        "maestro.gate_capacity.acquire_slot", fake_acquire,
    ), patch(
        "maestro.gate_capacity.count_in_flight", return_value=1,
    ), patch(
        "maestro.loop.pr_remediation.subprocess.run", side_effect=_all_pass,
    ):
        result = await agent._pr_remediation.preflight(entry)

    assert result is True
    lines = mem_fs[str(agent.config.gate_runs_path)].strip().splitlines()
    assert len(lines) == 1
    record = gate_capacity._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "slot"
    assert record.outcome == "CLEAN"


@pytest.mark.asyncio
async def test_preflight_skip_counts_as_noop_and_escalates_stuck_pr(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A preflight-skip is recorded as a no-op; once the repetition gate
    trips while the PR is still unmerged, the PR is dequeued for good and a
    deterministic merge-blocker reason is surfaced via a best-effort
    notification — with no SDK query (``agent.run``) on this path."""
    agent = make_agent()
    # Pre-seed one prior no-op so the skip recorded inside ``_run_task_phase``
    # is the second consecutive no-op, tripping the default threshold of 2.
    agent.remediation_tracker.record_outcome(1, noop=True)

    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #1: fix lint\n\n## Problem\n"
                "After merging origin/main into PR #1, quality gates "
                "failed: ruff exit 1. Fix the underlying breakage."
            ),
            pr_branch="maestro/evolve/p1",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    # The PR is still open, CI is green, branch is current — but review is
    # outstanding (``BLOCKED``) → AWAITING_APPROVAL.
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": PrMergeState(
            number=1, head_ref="maestro/evolve/p1", title="t", body="b",
            state="OPEN", mergeable="MERGEABLE", merge_state_status="BLOCKED",
            review_decision="REVIEW_REQUIRED", created_at="", updated_at="",
            url="",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (1, 0)

    def _all_pass(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        stdout = _preflight_git_stdout(cmd, "abc123")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_all_pass):
        await agent._run_task_phase()

    # No SDK query on this path.
    agent.run.assert_not_awaited()

    # The PR was dequeued and not re-added.
    after = agent.consciousness.load_self_model()
    assert after.pending_pr_requests == []

    # The repetition gate now refuses to re-route this PR.
    assert agent.remediation_tracker.should_reroute(1) is False

    # The deterministic merge-blocker reason was surfaced to the operator.
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "PR #1" in message
    assert "AWAITING_APPROVAL" in message


@pytest.mark.asyncio
async def test_preflight_skip_no_escalation_below_threshold(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A single preflight-skip records a no-op but does not escalate while
    the consecutive-no-op count is below threshold."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #3: fix lint\n\n## Problem\n"
                "After merging origin/main into PR #3, quality gates "
                "failed: ruff exit 1."
            ),
            pr_branch="maestro/evolve/p3",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

    def _all_pass(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        stdout = _preflight_git_stdout(cmd, "abc123")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_all_pass):
        await agent._run_task_phase()

    # One no-op recorded — still below threshold, so no escalation fired.
    agent._notify_best_effort.assert_not_awaited()
    assert agent.remediation_tracker.should_reroute(3) is True


@pytest.mark.asyncio
async def test_record_preflight_skip_outcome_ignores_unparseable_goal(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A goal that is not a ``Remediate PR #<n>:`` directive records no
    outcome and never escalates."""
    agent = make_agent()
    agent._escalate_stuck_pr = AsyncMock()  # type: ignore[method-assign]
    await agent._record_preflight_skip_outcome(
        "Some unrelated goal", "maestro/evolve/x",
    )
    agent._escalate_stuck_pr.assert_not_awaited()
    # No tracker entry was written for any PR.
    assert agent.remediation_tracker.should_reroute(1) is True


@pytest.mark.asyncio
async def test_escalate_stuck_pr_skips_when_already_merged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """If the PR merged in the meantime, the loop the gate guards against
    has already broken — no notification is emitted."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = True
    agent.evolution._host = host  # type: ignore[attr-defined]
    await agent._escalate_stuck_pr(1, "maestro/evolve/p1")
    agent._notify_best_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalate_stuck_pr_without_branch_uses_ci_state_only(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With no branch, the blocker is derived from the CI conclusion alone
    (no commits-behind / merge-state lookup)."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    agent.evolution._host = host  # type: ignore[attr-defined]
    await agent._escalate_stuck_pr(5, None)
    host.get_open_evolution_pr_states.assert_not_called()
    agent._notify_best_effort.assert_awaited_once()
    assert "CI_FAILING" in agent._notify_best_effort.await_args.args[0]


@pytest.mark.asyncio
async def test_escalate_stuck_pr_stamps_ledger_escalated_at(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A fired escalation flight-records ``escalated_at`` on the PR-ledger
    entry, so the ledger shows whether the operator was already notified
    for this stuck episode; a merged PR (escalation suppressed) does not."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent.pr_ledger.observe_head(5, "maestro/evolve/p5", "sha1")
    await agent._escalate_stuck_pr(5, None)
    entry = agent.pr_ledger.get(5)
    assert entry is not None
    assert entry.escalated_at is not None

    # Merged in the meantime → the early return skips the stamp too.
    host.is_pr_merged.return_value = True
    agent.pr_ledger.observe_head(6, "maestro/evolve/p6", "sha1")
    await agent._escalate_stuck_pr(6, "maestro/evolve/p6")
    entry6 = agent.pr_ledger.get(6)
    assert entry6 is not None
    assert entry6.escalated_at is None


@pytest.mark.asyncio
async def test_escalate_stuck_pr_tolerates_missing_merge_state(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the PR branch is absent from the open-PR snapshot, merge-state
    stays empty and the blocker falls back to commits-behind."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {}
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 3)
    await agent._escalate_stuck_pr(6, "maestro/evolve/p6")
    agent._notify_best_effort.assert_awaited_once()
    assert "BEHIND_MAIN" in agent._notify_best_effort.await_args.args[0]


@pytest.mark.asyncio
async def test_escalate_stuck_pr_delivers_durable_handoff(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A git-unmerged stuck PR is durably handed up (#2783): exactly one
    ``pr_number``-keyed standdown + one merge-or-close message, in addition to
    the head-scoped ledger stamp and best-effort notify. Idempotent across a
    head-advance — the standdown is keyed on the PR, not the head SHA, so the
    bug (the old ``mark_escalated`` flag wiped on every head move) cannot
    recur."""
    from maestro import escalation_bridge

    agent = make_agent()
    agent.config.pr_remediation_lap_budget = 3
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    agent.evolution._host = host  # type: ignore[attr-defined]
    # Two recorded no-ops → ``noop_count`` reports the stuck depth as 2.
    agent.remediation_tracker.record_outcome(5, noop=True)
    agent.remediation_tracker.record_outcome(5, noop=True)
    agent._vcs.rev_parse.return_value = "head-a"

    await agent._escalate_stuck_pr(5, "maestro/evolve/p5")

    mem = agent.config.memory_dir
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5) is True
    raw = mem_fs.get(str(mem / "messages.jsonl"), "")
    msgs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(msgs) == 1
    assert "#5" in msgs[0]["content"]
    # The existing head-scoped stamp + notify still fire alongside the handoff.
    agent._notify_best_effort.assert_awaited_once()

    # A head-advance (the churn that wiped the old flag) does not re-knock.
    agent._vcs.rev_parse.return_value = "head-b"
    await agent._escalate_stuck_pr(5, "maestro/evolve/p5")
    raw2 = mem_fs.get(str(mem / "messages.jsonl"), "")
    msgs2 = [json.loads(line) for line in raw2.splitlines() if line.strip()]
    assert len(msgs2) == 1
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5) is True


@pytest.mark.asyncio
async def test_escalate_stuck_pr_handoff_names_capability(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Criterion 4 — the lap-budget escalation caller obtains the *enriched*
    merge-or-close message through the single composer: when the handed-off PR
    resolves to an owning plan, ``_escalate_stuck_pr`` threads
    ``plan_lookup=self._plan_for_pr_number`` so the capability line naming what
    the merge would land is prepended, rather than the generic body alone."""
    from maestro.escalation_bridge import has_standdown
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.pr_remediation_lap_budget = 3
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent.remediation_tracker.record_outcome(5, noop=True)
    agent._vcs.rev_parse.return_value = "head-a"
    # The handed-off PR resolves to an owning plan carrying a first acceptance
    # criterion — the capability the merge would land.
    plan = EvolutionPlan(
        plan_id="ep-cap-5",
        title="Owning plan title",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/escalation_bridge.py"],
        expected_impact="x",
        acceptance_criteria=["The merge lands the legible handoff line"],
    )
    agent._plan_for_pr_number = MagicMock(  # type: ignore[method-assign]
        return_value=plan,
    )

    await agent._escalate_stuck_pr(5, "maestro/evolve/p5")

    assert has_standdown(agent.escalation_standdowns, 5) is True
    mem = agent.config.memory_dir
    raw = mem_fs.get(str(mem / "messages.jsonl"), "")
    msgs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(msgs) == 1
    assert (
        "This merge would land: The merge lands the legible handoff line"
        in msgs[0]["content"]
    )
    assert "#5" in msgs[0]["content"]


@pytest.mark.asyncio
async def test_escalate_stuck_pr_disabled_budget_no_handoff(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """With ``pr_remediation_lap_budget <= 0`` the durable hand-up is fully
    disabled — no standdown, no message — though the head-scoped notify still
    fires (#2783)."""
    from maestro import escalation_bridge

    agent = make_agent()
    agent.config.pr_remediation_lap_budget = 0
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    agent.evolution._host = host  # type: ignore[attr-defined]

    await agent._escalate_stuck_pr(5, "maestro/evolve/p5")

    mem = agent.config.memory_dir
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5) is False
    assert mem_fs.get(str(mem / "messages.jsonl"), "") == ""
    agent._notify_best_effort.assert_awaited_once()


def _pr_state(branch: str, *, mergeable: str, status: str) -> PrMergeState:
    """Build a minimal ``PrMergeState`` for the host-probe tests (#2457)."""
    return PrMergeState(
        number=1, head_ref=branch, title="t", body="b", state="OPEN",
        mergeable=mergeable, merge_state_status=status,
        review_decision="", created_at="", updated_at="", url="",
    )


@pytest.mark.asyncio
async def test_probe_pr_merge_block_merged_is_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A merged PR is landed with no blocker — and no extra host/VCS probes."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = True
    agent.evolution._host = host  # type: ignore[attr-defined]
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is True
    assert reason == "NONE"
    # Merged short-circuits before any branch/CI lookup.
    agent._vcs.commits_ahead_behind.assert_not_called()
    host.get_open_evolution_pr_states.assert_not_called()


@pytest.mark.asyncio
async def test_probe_pr_merge_block_conflicting_not_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A host-side DIRTY/CONFLICTING PR has not landed even with green CI and
    no local-only commits — ``classify_merge_block`` names the conflict."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="CONFLICTING", status="DIRTY",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "CONFLICTING"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_mergeable_conflicting_without_dirty(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The host ``mergeable`` field can report CONFLICTING even when
    ``merge_state`` is not yet DIRTY (so ``classify_merge_block`` returns NONE);
    the probe refines that to CONFLICTING rather than escalating as NONE
    (#2457)."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="CONFLICTING", status="UNKNOWN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "CONFLICTING"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_unpushed_not_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Local-only resolution commits (branch ahead of its own origin ref) mean
    the resolution has not reached the host, so the PR has not landed."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    # First call: vs origin/main (not behind). Second: vs origin/<branch>
    # (one local commit not yet pushed).
    agent._vcs.commits_ahead_behind.side_effect = [(0, 0), (1, 0)]
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "AWAITING_PUSH"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_missing_origin_ref_not_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A branch whose ``origin/<branch>`` ref is unresolvable was never pushed,
    so the resolution has not landed — even though ``commits_ahead_behind``
    collapses the missing ref to ``(0, 0)`` and every other signal reads clean
    (#2457 fail-open guard)."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    # The origin ref does not resolve — the branch was never pushed.
    agent._vcs.rev_parse = MagicMock(return_value=None)
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "AWAITING_PUSH"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_clean_on_origin_is_landed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A cleanly-mergeable PR with the resolution already on origin is landed."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is True
    assert reason == "NONE"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_behind_and_local_conflict_reclassifies(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A branch behind ``origin/main`` whose local merge-tree probe conflicts is
    reclassified CONFLICTING (LLM-clearable), never left BEHIND_MAIN — even when
    the host ``mergeable`` field reads clean (the flapping-field bug 080 fixes).
    The probe is the in-memory ``origin/<branch>`` vs ``origin/main`` merge."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    # Behind origin/main, but pushed (no local-only commits).
    agent._vcs.commits_ahead_behind.side_effect = [(0, 3), (0, 0)]
    agent._vcs.merge_tree_conflicts.return_value = True
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "CONFLICTING"
    agent._vcs.merge_tree_conflicts.assert_called_once_with(
        "origin/maestro/evolve/p1", "origin/main",
    )


@pytest.mark.asyncio
async def test_probe_pr_merge_block_behind_and_clean_stays_behind_main(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A clean-but-behind branch (local merge-tree probe reports no conflict) is
    still BEHIND_MAIN — Tend PR catches it up mechanically."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.side_effect = [(0, 3), (0, 0)]
    agent._vcs.merge_tree_conflicts.return_value = False
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "BEHIND_MAIN"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_behind_and_ci_failing_is_ci_failing(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A behind branch whose head CI concluded ``failure`` reads CI_FAILING, not
    BEHIND_MAIN — Tend refuses to catch up a red head, so the merge-block gate
    must not evict the remediation Tend routed (#4096)."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="BEHIND",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.side_effect = [(0, 3), (0, 0)]
    agent._vcs.merge_tree_conflicts.return_value = False
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "CI_FAILING"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_behind_ci_failing_unpushed_stays_behind_main(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A committed-but-unpushed fix on a behind red head stays BEHIND_MAIN:
    Tend's red-head route yields to it and pushes it (#4096 escape hatch)."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="BEHIND",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.side_effect = [(0, 3), (2, 0)]
    agent._vcs.merge_tree_conflicts.return_value = False
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "BEHIND_MAIN"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_behind_conflicting_and_ci_failing_is_conflicting(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A host conflict outranks the red-head refinement: catching up is required
    either way and only the LLM can do it (#2521 over #4096)."""
    agent = make_agent()
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="CONFLICTING", status="BEHIND",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.side_effect = [(0, 3), (0, 0)]
    agent._vcs.merge_tree_conflicts.return_value = False
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "CONFLICTING"


@pytest.mark.asyncio
async def test_probe_pr_merge_block_local_probe_disabled_skips(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With ``merge_block_local_probe_enabled`` off, a BEHIND_MAIN verdict is
    never probed locally — it stays BEHIND_MAIN regardless of conflict state."""
    agent = make_agent()
    agent.config.merge_block_local_probe_enabled = False
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.side_effect = [(0, 3), (0, 0)]
    # Even if a local merge would conflict, the disabled probe is never consulted.
    agent._vcs.merge_tree_conflicts.return_value = True
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert landed_ok is False
    assert reason == "BEHIND_MAIN"
    agent._vcs.merge_tree_conflicts.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conclusion", "unpushed", "conflicts", "expected", "landed"),
    [
        ("success", 0, False, "NONE", True),
        ("pending", 0, False, "CI_PENDING", False),
        ("failure", 0, False, "CI_FAILING", False),
        ("success", 2, False, "AWAITING_PUSH", False),
        ("success", 0, True, "CONFLICTING", False),
    ],
)
async def test_probe_pr_merge_block_behind_under_merge_queue(
    make_agent,  # type: ignore[no-untyped-def]
    conclusion: str,
    unpushed: int,
    conflicts: bool,
    expected: str,
    landed: bool,
) -> None:
    """#4307: with the Tend catch-up off, behind-ness is no blocker — the
    verdict is the CI/push/conflict rung behind it, probed once on origin."""
    agent = make_agent()
    agent.config.tend_main_merge_enabled = False
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = conclusion
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p1": _pr_state(
            "maestro/evolve/p1", mergeable="MERGEABLE", status="BEHIND",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.side_effect = [(0, 3), (unpushed, 0)]
    agent._vcs.merge_tree_conflicts.return_value = conflicts
    landed_ok, reason = await agent._probe_pr_merge_block(1, "maestro/evolve/p1")
    assert reason == expected
    assert landed_ok is landed
    agent._vcs.merge_tree_conflicts.assert_called_once_with(
        "origin/maestro/evolve/p1", "origin/main",
    )


@pytest.mark.asyncio
async def test_escalate_stuck_pr_skips_when_cleanly_mergeable(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A serviced PR that is now cleanly mergeable (not merged) has no blocker
    to escalate — the helper reports landed, so no notification fires."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p7": _pr_state(
            "maestro/evolve/p7", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    await agent._escalate_stuck_pr(7, "maestro/evolve/p7")
    agent._notify_best_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalate_stuck_pr_awaiting_push_names_owned_push(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An AWAITING_PUSH resolution is my own next act (proposal 098) — one push
    lands it — so, for a blocker with a branch, the escalation names the owned
    push command (``git push origin HEAD:<branch>``) rather than a Tend deferral
    or manual action (#2457)."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p8": _pr_state(
            "maestro/evolve/p8", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    # Clean vs origin/main, but one local-only commit not yet pushed.
    agent._vcs.commits_ahead_behind.side_effect = [(0, 0), (1, 0)]
    await agent._escalate_stuck_pr(8, "maestro/evolve/p8")
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "AWAITING_PUSH" in message
    assert "not yet pushed" in message
    assert "Manual action required" not in message
    assert "git push origin HEAD:maestro/evolve/p8" in message
    assert "check why Tend PR has not pushed it" not in message


@pytest.mark.asyncio
async def test_escalate_stuck_pr_awaiting_push_without_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """AWAITING_PUSH but no branch: fall back to the Tend wording, never a
    ``None`` command.

    ``_probe_pr_merge_block`` only derives AWAITING_PUSH inside an
    ``if pr_branch:`` comparison, so this pairing is unreachable today — the
    probe is mocked to force it. The ``if pr_branch:`` guard is what keeps the
    operator-facing git command from interpolating a literal ``None`` if that
    probe invariant ever changes.
    """
    from maestro.remediation_tracker import MERGE_BLOCK_AWAITING_PUSH

    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    agent._probe_pr_merge_block = AsyncMock(  # type: ignore[method-assign]
        return_value=(False, MERGE_BLOCK_AWAITING_PUSH),
    )

    await agent._escalate_stuck_pr(8, None)

    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "AWAITING_PUSH" in message
    assert "None" not in message
    assert "git push origin" not in message
    assert "check why Tend PR has not pushed it" in message


def test_frame_remediation_note_with_existing_findings(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The note is prepended to both summary and non-empty key findings."""
    agent = make_agent()
    summary, findings = agent._frame_remediation_note(
        "Original summary.", "Patched the gate.", "BLOCKER NOTE.",
    )
    assert summary == "BLOCKER NOTE. Original summary."
    assert findings == "BLOCKER NOTE. Patched the gate."


def test_frame_remediation_note_without_findings(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When distillation yielded no findings, the note alone becomes them and
    the summary stays within the 500-char cap."""
    agent = make_agent()
    long_summary = "x" * 600
    summary, findings = agent._frame_remediation_note(
        long_summary, None, "NOTE.",
    )
    assert findings == "NOTE."
    assert len(summary) == 500
    assert summary.startswith("NOTE. ")


@pytest.mark.asyncio
async def test_run_remediation_unlanded_downgrades_to_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An all-green remediation whose PR has not reached a mergeable/merged
    host state is downgraded ``success`` -> ``partial`` and honestly framed
    with the deterministic merge-block reason (#2457)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"  # CI_FAILING
    host.get_open_evolution_pr_states.return_value = {}
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Resolved the conflict.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #91: fix CI", source="external",
            pr_branch="maestro/evolve/ep91",
        )

    assert record.outcome == "partial"
    assert "CI_FAILING" in record.result_summary
    assert "CI_FAILING" in (record.key_findings or "")
    # The §5-push succeeded (default mock), so the pre-push awaiting-tend-push
    # marker was rewritten to the delivered marker (proposal 091).
    assert "delivered to pr, awaiting review/merge" in record.result_summary
    assert agent._last_remediation_merge_block == "CI_FAILING"


@pytest.mark.asyncio
async def test_run_remediation_landed_keeps_success(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An all-green remediation whose PR has merged keeps ``success`` and the
    host-verdict attribute stays ``None`` so the repetition gate sees real
    work (#2457)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    host = MagicMock()
    host.is_pr_merged.return_value = True
    agent.evolution._host = host  # type: ignore[attr-defined]
    # Merge-truth chokepoint agrees the branch landed so the default-on §5d
    # mechanical-outcome authority keeps the verdict ``success`` (proposal 105).
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=92, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Done.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #92: fix CI", source="external",
            pr_branch="maestro/evolve/ep92",
        )

    assert record.outcome == "success"
    assert agent._last_remediation_merge_block is None


@pytest.mark.asyncio
async def test_run_remediation_unparseable_number_skips_gate(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A remediation-prefixed goal with no parseable PR number never probes the
    host and is left at its step-based outcome (defensive guard, #2457)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    host = MagicMock()
    agent.evolution._host = host  # type: ignore[attr-defined]

    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Done.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #notanumber: fix", source="external",
            pr_branch="maestro/evolve/epx",
        )

    assert record.outcome == "success"
    assert agent._last_remediation_merge_block is None
    host.is_pr_merged.assert_not_called()


@pytest.mark.asyncio
async def test_run_remediation_downgrade_preserves_branch_suffix(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A downgraded remediation with long step output keeps both the prepended
    host-verdict note and the trailing ``Branch:`` suffix within the 500-char
    cap — the framing budget reserves room for the note so the branch suffix is
    not truncated off the end (#2457)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"  # CI_FAILING
    host.get_open_evolution_pr_states.return_value = {}
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    # The remediation commits a real fix on the PR branch, so run() keeps the
    # branch and appends the ``Branch:`` suffix to the summary.
    agent._vcs.has_changes.return_value = True

    plan_response = json.dumps([{"description": "Step 1"}])
    long_output = "x" * 1000  # overruns the 500-char summary cap on its own.

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Resolved the conflict.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=long_output)
        record = await agent.run(
            "Remediate PR #93: fix CI", source="external",
            pr_branch="maestro/evolve/ep93",
        )

    assert record.outcome == "partial"
    assert len(record.result_summary) <= 500
    # The §5-push succeeded, so the marker was rewritten to the delivered form;
    # the branch suffix is still preserved past the 500-char cap (proposal 091).
    assert "delivered to pr, awaiting review/merge" in record.result_summary
    assert "Branch:" in record.result_summary


@pytest.mark.asyncio
async def test_preflight_remediation_proceeds_when_gates_fail(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When a quality gate fails on the *merged* tree (the branch is
    green standalone but red after merging origin/main), the full LLM
    remediation flow must be entered — preflight must not skip (#2402).

    The subprocess mock is command-aware: the probe-merge succeeds, but
    ``mypy`` fails on the merged tree. The probe-merge commit must be
    reset away and the tree returned to ``main`` before the LLM flow.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #2: fix tests\n\n## Problem\n"
                "After merging origin/main into PR #2, quality gates "
                "failed: pytest exit 1."
            ),
            pr_branch="maestro/evolve/p2",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    captured_calls: list[tuple[object, dict[str, object]]] = []

    def _mixed_subprocess(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured_calls.append((cmd, kwargs))
        # mypy fails on the merged tree → preflight returns False.
        if isinstance(cmd, list) and cmd == ["mypy", "maestro/", "scripts/"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="error", stderr="",
            )
        # ``git rev-parse HEAD`` reports a sha so the probe-merge commit
        # can be reset away in the ``finally`` block.
        stdout = _preflight_git_stdout(cmd, "deadbeef")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_mixed_subprocess):
        await agent._run_task_phase()

    # Full LLM flow must have been entered.
    agent.run.assert_awaited_once()
    _, kwargs = agent.run.await_args
    assert kwargs["pr_branch"] == "maestro/evolve/p2"
    assert kwargs["source"] == "external"

    cmds = [cmd for cmd, _ in captured_calls]
    # The probe-merge ran against origin/main (reproducing the post-merge
    # condition), gates ran on the merged tree, and the merge commit was
    # reset away before returning to main.
    assert ["git", "merge", "--no-ff", "--no-edit", "origin/main"] in cmds
    assert ["git", "reset", "--hard", "deadbeef"] in cmds
    # The `finally` block MUST return the working tree to ``main`` so
    # whatever runs next starts from a clean base, then *prove* it did:
    # the two verification reads are the tail of the sequence (#3715).
    checkout_index = cmds.index(["git", "checkout", "main"])
    assert cmds[checkout_index:] == [
        ["git", "checkout", "main"],
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "status", "--porcelain"],
    ]
    for _, kwargs_seen in captured_calls[checkout_index:]:
        assert str(kwargs_seen.get("cwd")) == str(agent.config.workspace)


@pytest.mark.asyncio
async def test_preflight_returns_false_on_post_merge_gate_failure(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Regression for #2402: a branch that passes gates in isolation but
    fails after merging ``origin/main`` must NOT be skipped.

    Called directly, ``_preflight_pr_remediation`` must probe-merge
    ``origin/main`` first and run the gates on the merged tree. Here the
    merge succeeds but ``pytest`` fails post-merge, so preflight returns
    ``False`` and the merge commit is reset away (never pushed).
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #8: post-merge failure\n\n## Problem\n"
            "After merging origin/main into PR #8, quality gates "
            "failed: pytest exit 1."
        ),
        pr_branch="maestro/evolve/p8",
    )

    captured: list[object] = []

    def _post_merge_fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        if isinstance(cmd, list) and cmd[:1] == ["pytest"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="fail", stderr="",
            )
        stdout = _preflight_git_stdout(cmd, "cafe01")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_post_merge_fail):
        result = await agent._preflight_pr_remediation(entry)

    assert result is False
    assert ["git", "merge", "--no-ff", "--no-edit", "origin/main"] in captured
    # The probe-merge commit is reset away and the tree returned to main.
    assert ["git", "reset", "--hard", "cafe01"] in captured
    assert ["git", "checkout", "main"] in captured


@pytest.mark.asyncio
async def test_preflight_returns_false_on_merge_conflict(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When the probe-merge conflicts, the branch is not mergeable, so
    genuine remediation is required: preflight returns ``False``, aborts
    the half-applied merge, and leaves the tree clean (#2402).
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #9: conflict\n\n## Problem\n"
            "After merging origin/main into PR #9, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p9",
    )

    captured: list[object] = []

    def _merge_conflicts(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        if isinstance(cmd, list) and cmd[:2] == ["git", "merge"] and "--abort" not in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="CONFLICT",
            )
        stdout = _preflight_git_stdout(cmd, "f00d02")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_merge_conflicts):
        result = await agent._preflight_pr_remediation(entry)

    assert result is False
    # The half-applied merge is aborted (which restores HEAD), so no
    # ``reset --hard`` of a merge commit is issued. No gates run.
    assert ["git", "merge", "--abort"] in captured
    assert not any(
        isinstance(c, list) and c[:1] == ["ruff"] for c in captured
    )
    assert not any(
        isinstance(c, list) and c[:3] == ["git", "reset", "--hard"]
        for c in captured
    )
    assert ["git", "checkout", "main"] in captured


@pytest.mark.asyncio
async def test_preflight_restoration_failure_fails_loud(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An ``OSError`` from the probe-merge reset must now surface (#3715).

    The inverse of the former ``test_preflight_reset_failure_is_tolerated``:
    restoring the *shared* worktree is a checked postcondition, not best-effort
    cleanup, so the failure propagates out of ``preflight`` instead of being
    swallowed into a ``True`` gate verdict on an unproven tree.
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #11: reset failure\n\n## Problem\n"
            "After merging origin/main into PR #11, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p11",
    )

    captured: list[object] = []

    def _reset_raises(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        if isinstance(cmd, list) and cmd[:3] == ["git", "reset", "--hard"]:
            raise OSError("disk full")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "beef03"), stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_reset_raises), \
         pytest.raises(OSError, match="disk full"):
        await agent._preflight_pr_remediation(entry)

    assert ["git", "reset", "--hard", "beef03"] in captured


@pytest.mark.asyncio
async def test_preflight_failed_merge_abort_keeps_reset_armed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A failed ``git merge --abort`` must not disarm the ``finally`` reset.

    Regression for #3715: the conflict arm used to clear ``pre_merge_head``
    unconditionally, so an unabortable merge left the tree half-applied *and*
    suppressed the one command that would have cleaned it. Now the clear is
    conditional on a zero exit, so the reset still fires.
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #12: unabortable conflict\n\n## Problem\n"
            "After merging origin/main into PR #12, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p12",
    )

    captured: list[object] = []

    def _abort_fails(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        if isinstance(cmd, list) and cmd[:2] == ["git", "merge"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="",
                stderr="CONFLICT" if "--abort" not in cmd else "index locked",
            )
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "ab0712"), stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_abort_fails):
        result = await agent._preflight_pr_remediation(entry)

    # The genuine-conflict verdict is unchanged: remediation is required.
    assert result is False
    assert ["git", "merge", "--abort"] in captured
    # ``pre_merge_head`` survived the failed abort, so the reset still ran.
    assert ["git", "reset", "--hard", "ab0712"] in captured


@pytest.mark.asyncio
async def test_preflight_nonzero_reset_raises_restoration_error(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A non-zero ``git reset --hard`` supersedes an all-green gate verdict.

    Every gate passes on the merged tree, so the body would have returned
    ``True`` and recorded a preflight pass — but the probe-merge commit could
    not be reset away, so no pass is recorded (#3715).
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #13: reset rejected\n\n## Problem\n"
            "After merging origin/main into PR #13, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p13",
    )

    def _reset_rejected(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        if isinstance(cmd, list) and cmd[:3] == ["git", "reset", "--hard"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128, stdout="", stderr="index.lock exists",
            )
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "cc1313"), stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_reset_rejected), \
         pytest.raises(PreflightRestorationError, match=r"index\.lock exists"):
        await agent._preflight_pr_remediation(entry)

    # No zero-cost preflight pass was stamped for this PR.
    assert agent.preflight_passes.should_skip(13, "cc1313") is False


@pytest.mark.asyncio
async def test_preflight_nonzero_checkout_main_raises_restoration_error(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A rejected return to ``main`` is fatal, not a DEBUG log line (#3715)."""
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #14: checkout rejected\n\n## Problem\n"
            "After merging origin/main into PR #14, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p14",
    )

    def _checkout_main_fails(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        if cmd == ["git", "checkout", "main"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="",
                stderr="local changes would be overwritten",
            )
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "dd1414"), stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_checkout_main_fails), \
         pytest.raises(
             PreflightRestorationError, match="would be overwritten",
         ):
        await agent._preflight_pr_remediation(entry)

    assert agent.preflight_passes.should_skip(14, "dd1414") is False


@pytest.mark.asyncio
async def test_preflight_unrestored_end_state_raises_restoration_error(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Both repair commands exit ``0`` yet the tree is not a clean ``main``.

    The verification reads are what catch this: a ``git checkout`` that
    reported success while HEAD stayed on the PR branch would otherwise leave
    the next phase mutating the wrong branch (#3715).
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #15: phantom restore\n\n## Problem\n"
            "After merging origin/main into PR #15, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p15",
    )

    def _phantom_restore(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        stdout = _preflight_git_stdout(cmd, "ee1515")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            stdout = "maestro/evolve/p15\n"
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_phantom_restore), \
         pytest.raises(
             PreflightRestorationError, match="maestro/evolve/p15",
         ):
        await agent._preflight_pr_remediation(entry)


@pytest.mark.asyncio
async def test_preflight_dirty_end_state_raises_restoration_error(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """HEAD is back on ``main`` but the worktree is dirty — still fatal."""
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #16: dirty restore\n\n## Problem\n"
            "After merging origin/main into PR #16, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p16",
    )

    status_reads: list[object] = []

    def _dirty_restore(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        stdout = _preflight_git_stdout(cmd, "ff1616")
        if cmd == ["git", "status", "--porcelain"]:
            status_reads.append(cmd)
            # The first read is the pre-probe baseline (clean); the dirt only
            # appears in the post-restoration read, so it is the probe's.
            if len(status_reads) > 1:
                stdout = "UU maestro/agent.py\n"
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_dirty_restore), \
         pytest.raises(
             PreflightRestorationError, match=r"UU maestro/agent\.py",
         ):
        await agent._preflight_pr_remediation(entry)


@pytest.mark.asyncio
async def test_preflight_conflict_enters_full_remediation_flow(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A conflict-on-merge during preflight routes the request into the
    full LLM remediation flow rather than skipping it (#2402)."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #10: conflict\n\n## Problem\n"
                "After merging origin/main into PR #10, quality gates "
                "failed: mypy exit 1."
            ),
            pr_branch="maestro/evolve/p10",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    def _merge_conflicts(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        if isinstance(cmd, list) and cmd[:2] == ["git", "merge"] and "--abort" not in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="CONFLICT",
            )
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "c0ff10"), stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_merge_conflicts):
        await agent._run_task_phase()

    agent.run.assert_awaited_once()
    _, kwargs = agent.run.await_args
    assert kwargs["pr_branch"] == "maestro/evolve/p10"


@pytest.mark.asyncio
async def test_preflight_remediation_fallback_on_error(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A gate subprocess ``OSError`` degrades to ``False`` without crashing.

    The *gate* verdict still degrades safely (#2402): an error inside the
    preflight body is swallowed into ``False`` and the full LLM remediation
    flow proceeds. Only a failure to restore the shared worktree is fatal
    (#3715), so here the git repair commands succeed.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #3: fix crash\n\n## Problem\n"
                "After merging origin/main into PR #3, quality gates "
                "failed: mypy exit 1."
            ),
            pr_branch="maestro/evolve/p3",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    def _gate_raises(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        if isinstance(cmd, list) and cmd[:1] in (["ruff"], ["mypy"], ["pytest"]):
            raise OSError("no ruff")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "0e0e03"), stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_gate_raises):
        await agent._run_task_phase()

    # Full LLM flow must have been entered despite the OS error.
    agent.run.assert_awaited_once()
    _, kwargs = agent.run.await_args
    assert kwargs["pr_branch"] == "maestro/evolve/p3"


@pytest.mark.asyncio
async def test_preflight_git_unavailable_stands_down_restoration(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Git unavailable *before* the probe moves anything owes no restoration.

    The restoration postcondition is scoped to what the probe owns (#3715):
    when even the baseline read raises, the PR-branch checkout never ran, so
    the shared worktree is exactly as preflight found it. The gate verdict
    degrades to ``False`` and the full LLM flow proceeds rather than the
    ``finally`` block manufacturing a crash over a tree it never touched.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #3: fix crash\n\n## Problem\n"
                "After merging origin/main into PR #3, quality gates "
                "failed: mypy exit 1."
            ),
            pr_branch="maestro/evolve/p3",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    with patch("maestro.agent.subprocess.run", side_effect=OSError("no git")):
        await agent._run_task_phase()

    agent.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_failed_branch_checkout_skips_restoration(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A rejected PR-branch checkout leaves the ``finally`` block standing down.

    The probe never moved the shared worktree, so it issues no repair command
    and asserts no end state — pre-existing dirt or a foreign HEAD belongs to
    whatever ran before preflight, not to preflight (#3715).
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #17: unreachable branch\n\n## Problem\n"
            "After merging origin/main into PR #17, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p17",
    )

    captured: list[object] = []

    def _checkout_branch_fails(
        *args: object, **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        if cmd == ["git", "checkout", "maestro/evolve/p17"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="no such branch")
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "aa1717"), stderr="",
        )

    with patch(
        "maestro.agent.subprocess.run", side_effect=_checkout_branch_fails,
    ):
        result = await agent._preflight_pr_remediation(entry)

    assert result is False
    # Only the baseline read and the failed checkout ran — no repair command.
    assert captured == [
        ["git", "status", "--porcelain"],
        ["git", "checkout", "maestro/evolve/p17"],
    ]


@pytest.mark.asyncio
async def test_preflight_tolerates_pre_existing_workspace_dirt(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Dirt the workspace already carried is not the probe's restoration debt.

    A clean tree is a precondition preflight never established (#3715), so the
    baseline entry measured before the PR-branch checkout is subtracted from
    the end-state reading and the all-green verdict survives.
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #18: pre-existing dirt\n\n## Problem\n"
            "After merging origin/main into PR #18, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p18",
    )

    def _always_dirty(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        stdout = _preflight_git_stdout(cmd, "bb1818")
        if cmd == ["git", "status", "--porcelain"]:
            stdout = "?? scratch.txt\n"
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_always_dirty):
        result = await agent._preflight_pr_remediation(entry)

    assert result is True
    # The gate pass is still stamped: restoration was proven for what the
    # probe itself left behind.
    assert agent.preflight_passes.should_skip(18, "bb1818") is True


@pytest.mark.asyncio
async def test_preflight_cancellation_skips_restoration_and_propagates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Cancellation reaches the loop intact rather than becoming a git verdict.

    The ``finally`` block stands down when the probe unwinds on
    ``CancelledError`` (#3715): graceful shutdown must not be replaced by a
    ``PreflightRestorationError`` raised on the way out.
    """
    agent = make_agent()
    entry = PendingPRRequest(
        directive=(
            "Remediate PR #19: cancelled mid-probe\n\n## Problem\n"
            "After merging origin/main into PR #19, quality gates "
            "failed: ruff exit 1."
        ),
        pr_branch="maestro/evolve/p19",
    )

    captured: list[object] = []

    def _cancel_on_merge(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cmd = args[0] if args else None
        captured.append(cmd)
        if isinstance(cmd, list) and cmd[:2] == ["git", "merge"]:
            raise asyncio.CancelledError
        return subprocess.CompletedProcess(
            args=cmd or [], returncode=0,
            stdout=_preflight_git_stdout(cmd, "cc1919"), stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_cancel_on_merge), \
         pytest.raises(asyncio.CancelledError):
        await agent._preflight_pr_remediation(entry)

    # No repair command ran: the cancellation was not traded for a git error.
    assert ["git", "checkout", "main"] not in captured


@pytest.mark.asyncio
async def test_preflight_skipped_for_changes_requested_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """CHANGES_REQUESTED entries must bypass the preflight gate.

    Reviewer-feedback problems are typically design/correctness issues
    invisible to ruff/mypy/pytest, so green branch gates do not imply
    the request is resolved. The full LLM remediation flow must run
    even when subprocess gates would pass.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #4: reviewer concerns\n\n## Problem\n"
                "A reviewer requested changes on PR #4. Fetch the latest "
                "review comments and address each blocking thread."
            ),
            pr_branch="maestro/evolve/p4",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("maestro.agent.subprocess.run", return_value=ok) as sp:
        await agent._run_task_phase()

    # Preflight must NOT have run (no subprocess git/ruff/mypy/pytest calls).
    assert sp.call_count == 0
    # Full LLM flow must have been entered.
    agent.run.assert_awaited_once()
    _, kwargs = agent.run.await_args
    assert kwargs["pr_branch"] == "maestro/evolve/p4"


@pytest.mark.asyncio
async def test_preflight_skipped_for_conflict_routed_directive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Conflict-routed entries must bypass the preflight gate.

    Branch-local ruff/mypy/pytest can pass while ``origin/main`` still
    refuses to merge, so a green preflight does not mean the conflict
    is resolved.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #5: merge conflict\n\n## Problem\n"
                "PR #5 conflicts with origin/main. Resolve by merging "
                "origin/main into this branch and fixing the conflicts. "
                "Commit the resolution on this branch. Do NOT push."
            ),
            pr_branch="maestro/evolve/p5",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("maestro.agent.subprocess.run", return_value=ok) as sp:
        await agent._run_task_phase()

    assert sp.call_count == 0
    agent.run.assert_awaited_once()
    _, kwargs = agent.run.await_args
    assert kwargs["pr_branch"] == "maestro/evolve/p5"


@pytest.mark.asyncio
async def test_preflight_skipped_when_directive_mixes_quality_gates_and_review(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When a single PR aggregates quality-gates + reviewer-feedback,
    the reviewer concerns still need addressing even if gates are
    green — preflight must not skip.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #7: mixed problems\n\n## Problem\n"
                "After merging origin/main into PR #7, quality gates "
                "failed: ruff exit 1.\n\n"
                "A reviewer requested changes on PR #7."
            ),
            pr_branch="maestro/evolve/p7",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("maestro.agent.subprocess.run", return_value=ok) as sp:
        await agent._run_task_phase()

    assert sp.call_count == 0
    agent.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_returns_false_when_pr_branch_is_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``_preflight_pr_remediation`` must short-circuit to False when
    the request carries no ``pr_branch`` — there is nothing to check
    out, so the safe behaviour is to defer to the LLM flow.
    """
    agent = make_agent()
    entry = PendingPRRequest(directive="ignored", pr_branch=None)
    result = await agent._preflight_pr_remediation(entry)
    assert result is False


@pytest.mark.asyncio
async def test_preflight_skipped_when_directive_mixes_quality_gates_and_conflict(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When a single PR aggregates quality-gates + conflict problems,
    the conflict portion still needs work even if gates are green —
    preflight must not skip.
    """
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=(
                "Remediate PR #6: mixed problems\n\n## Problem\n"
                "After merging origin/main into PR #6, quality gates "
                "failed: ruff exit 1.\n\n"
                "PR #6 conflicts with origin/main. Resolve by merging."
            ),
            pr_branch="maestro/evolve/p6",
        ),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock()  # type: ignore[method-assign]

    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("maestro.agent.subprocess.run", return_value=ok) as sp:
        await agent._run_task_phase()

    assert sp.call_count == 0
    agent.run.assert_awaited_once()


# ──────────────────────────────────────────────────────────────────────
# Merge-truth-first stand-down preflight (third merge-truth face, #2579)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_stands_down_on_merged_pr(make_agent):  # type: ignore[no-untyped-def]
    """A user-request goal referencing an already-merged PR stands down as the
    literal first action: ``[stood-down]`` marker, no SDK servicing, no
    ``success`` stamp, and the no-op record is persisted (#2579)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    # The preflight reads merge-truth through the is_pr_settled chokepoint
    # (proposal 087); a MERGED observation settles the stand-down.
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2579, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("servicing query issued on a stand-down")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=boom), \
         patch("maestro.agent.query_on_session", side_effect=boom):
        record = await agent.run(
            "Please check on PR #2579 and remediate it", source="external",
        )

    assert record.outcome != "success"
    assert record.result_summary == "[stood-down]"
    assert record.plan == []
    assert record.branch is None
    mock_plan.assert_not_called()
    mock_exec.assert_not_called()
    agent.is_pr_settled.assert_called_once_with(2579)
    # The no-op record is persisted so the FORCED-QUIET-RATIO mirror can see it.
    recent = agent.memory.get_recent_tasks(1)
    assert len(recent) == 1
    assert recent[0].result_summary == "[stood-down]"


@pytest.mark.asyncio
async def test_run_stands_down_on_closed_pr(make_agent):  # type: ignore[no-untyped-def]
    """A goal referencing a closed-but-unmerged PR also stands down — the
    closed fork is settled, so servicing it is wasted work (#2579)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    # A CLOSED-unmerged observation is settled (not landed) → the chokepoint
    # stands the preflight down (proposal 087).
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2611, settled=True, state="CLOSED",
            branch_landed=False, source="host_api", observed_at=0.0,
        ),
    )

    async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("servicing query issued on a stand-down")

    with patch("maestro.planner.query", new_callable=AsyncMock), \
         patch("maestro.executor.query", new_callable=AsyncMock), \
         patch("maestro.agent.query", side_effect=boom), \
         patch("maestro.agent.query_on_session", side_effect=boom):
        record = await agent.run(
            "Resolve PR 2611 for me", source="external",
        )

    assert record.outcome != "success"
    assert record.result_summary == "[stood-down]"
    agent.is_pr_settled.assert_called_once_with(2611)


@pytest.mark.asyncio
async def test_run_no_standdown_on_open_pr(make_agent):  # type: ignore[no-untyped-def]
    """An open (neither merged nor closed) referenced PR is a no-op for the
    preflight: normal servicing proceeds to a step-based ``success``."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent._fetch_origin_and_fast_forward = MagicMock()  # type: ignore[method-assign]
    agent.evolution._host.is_pr_merged = MagicMock(  # type: ignore[attr-defined]
        return_value=False,
    )
    agent.evolution._host.is_pr_closed = MagicMock(  # type: ignore[attr-defined]
        return_value=False,
    )
    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Findings.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Investigate PR #2700 behaviour", source="external",
        )

    assert record.outcome == "success"
    assert record.result_summary != "[stood-down]"
    mock_plan.assert_called()


@pytest.mark.asyncio
async def test_run_no_standdown_without_pr_reference(make_agent):  # type: ignore[no-untyped-def]
    """A goal with no PR reference never consults merge-truth — the preflight
    short-circuits on the regex miss and normal servicing proceeds."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent.evolution._host.is_pr_merged = MagicMock(  # type: ignore[attr-defined]
        return_value=False,
    )
    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Findings.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run("Improve the dashboard layout", source="external")

    assert record.outcome == "success"
    assert record.result_summary != "[stood-down]"
    agent.evolution._host.is_pr_merged.assert_not_called()


@pytest.mark.asyncio
async def test_run_remediation_branch_skips_standdown_preflight(  # type: ignore[no-untyped-def]
    make_agent,
):
    """The preflight is scoped to ``pr_branch is None``: a remediation run on a
    merged PR branch is left to the dequeue-face guards / stamp gate, not stood
    down here (so existing servicing semantics are untouched)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent.evolution._host.is_pr_merged = MagicMock(  # type: ignore[attr-defined]
        return_value=True,
    )
    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Done.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Remediate PR #2579: fix CI", source="external",
            pr_branch="maestro/evolve/ep2579",
        )

    # Preflight did not fire (pr_branch set); the run serviced and reached the
    # stamp gate rather than emitting the stand-down marker.
    assert record.result_summary != "[stood-down]"
    mock_plan.assert_called()


@pytest.mark.asyncio
async def test_run_standdown_classifies_as_chosen_quiet(  # type: ignore[no-untyped-def]
    make_agent,
):
    """The stand-down record classifies as a *chosen* quiet (never forced) and
    its outcome is not ``success`` — the deliberate silence the
    FORCED-QUIET-RATIO mirror was built to recognise (#2579).

    Uses a production-realistic *user-channel* goal ("Please check on PR
    #2579...") with no ``pr_branch``: this is the shape that actually reaches the
    preflight (``pr_branch is None``) and emits ``[stood-down]``. A
    ``Remediate PR #<n>:`` goal would, in production, always carry ``pr_branch``
    (dispatched from ``pending_pr_requests``) and skip the preflight entirely, so
    it can never produce this record. The mirror counts the stand-down as chosen
    on the marker alone — it must not require the remediation-lineage goal (#2662).
    """
    from dataclasses import asdict

    from maestro.remediation_tracker import measure_forced_vs_chosen_quiet

    agent = make_agent()
    agent.config.enable_session_reuse = False
    # A MERGED observation settles the stand-down through the chokepoint (087).
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=2579, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )

    async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("servicing query issued on a stand-down")

    with patch("maestro.planner.query", new_callable=AsyncMock), \
         patch("maestro.executor.query", new_callable=AsyncMock), \
         patch("maestro.agent.query", side_effect=boom), \
         patch("maestro.agent.query_on_session", side_effect=boom):
        record = await agent.run(
            "Please check on PR #2579 and remediate it if needed",
            source="external",
        )

    # The user-channel goal does NOT match the strict ^Remediate PR #<n>: lineage.
    from maestro.remediation_tracker import _REMEDIATION_GOAL_PATTERN
    assert _REMEDIATION_GOAL_PATTERN.match(record.goal) is None
    assert record.result_summary == "[stood-down]"
    assert record.outcome != "success"
    ratio = measure_forced_vs_chosen_quiet([asdict(record)])
    assert ratio.chosen == 1
    assert ratio.forced == 0


@pytest.mark.asyncio
async def test_run_no_standdown_on_word_ending_in_pr(make_agent):  # type: ignore[no-untyped-def]
    """The PR-reference regex requires a word boundary before the ``PR`` token,
    so a goal like 'Refactor expr 3 evaluation' must NOT extract PR #3 from the
    'pr 3' embedded in 'expr'. Without the boundary the preflight would consult
    merge-truth for a spurious low PR number (almost always merged) and stand
    the real task down with zero servicing (#2662).
    """
    agent = make_agent()
    agent.config.enable_session_reuse = False
    # Would force a stand-down IF the regex spuriously extracted a PR number.
    agent.evolution._host.is_pr_merged = MagicMock(  # type: ignore[attr-defined]
        return_value=True,
    )
    plan_response = json.dumps([{"description": "Step 1"}])

    async def mock_q(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response="Findings.")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=mock_q), \
         patch("maestro.agent.query_on_session", side_effect=mock_q):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step completed")
        record = await agent.run(
            "Refactor expr 3 evaluation", source="external",
        )

    # The embedded 'pr 3' is not a PR reference: merge-truth is never consulted
    # and the real task is serviced to a normal success.
    assert record.outcome == "success"
    assert record.result_summary != "[stood-down]"
    agent.evolution._host.is_pr_merged.assert_not_called()


def test_stand_down_marker_is_canonical_chosen_quiet_marker() -> None:
    """``_STAND_DOWN_MARKER`` must BE the canonical
    ``remediation_tracker._CHOSEN_QUIET_MARKER`` (reused, not re-declared) so
    the chosen-quiet mirror can never silently stop counting stand-downs if the
    canonical literal changes (#2662)."""
    from maestro.agent import _STAND_DOWN_MARKER
    from maestro.remediation_tracker import _CHOSEN_QUIET_MARKER

    assert _STAND_DOWN_MARKER == _CHOSEN_QUIET_MARKER
