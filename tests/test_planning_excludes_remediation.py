"""Evolution planning's recent-task window excludes ``Remediate PR #<n>:`` laps.

Behind ``[loop] planning_excludes_remediation_enabled`` both planning reads —
``EvolutionEngine.reflect`` and ``_validate_source_task_id`` — go through
``remediation_tracker.recent_planning_tasks``, which drops remediation laps
whose kept branch is an open PR head rather than a promotable task branch.
"""

from __future__ import annotations

from pathlib import Path

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import Memory, TaskRecord
from maestro.notifications.base import NullNotifier
from maestro.remediation_tracker import planning_base_branch, recent_planning_tasks
from tests.support.gap_fuel import GAP

_DIR = Path("/fake/planning_excl")


def _make(mem_fs: dict[str, str], *, enabled: bool) -> tuple[EvolutionEngine, Memory]:
    m = Memory(_DIR)
    c = Consciousness(_DIR)
    c.load_self_model()
    j = Journal(_DIR)
    config = Config(planning_excludes_remediation_enabled=enabled)
    e = EvolutionEngine(
        m, c, j, NullNotifier(), issues=IssueTracker(github_repo=None), config=config,
    )
    return e, m


def _rec(task_id: str, goal: str, branch: str | None = None) -> TaskRecord:
    return TaskRecord(
        task_id=task_id, goal=goal, plan=[], outcome="success",
        result_summary="r", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", branch=branch,
    )


def test_helper_flag_off_returns_window_unchanged(mem_fs: dict[str, str]) -> None:
    _e, m = _make(mem_fs, enabled=False)
    m.record_task(_rec("a", "Remediate PR #12: t"))
    m.record_task(_rec("b", "real work"))
    ids = [t.task_id for t in recent_planning_tasks(m, Config())]
    assert ids == ["a", "b"]


def test_helper_flag_on_drops_only_remediation_laps(mem_fs: dict[str, str]) -> None:
    _e, m = _make(mem_fs, enabled=True)
    m.record_task(_rec("a", "real work"))
    m.record_task(_rec("b", "Remediate PR #12: t"))
    m.record_task(_rec("c", ""))
    m.record_task(_rec("d", "Follow up on Remediate PR #3: x"))
    config = Config(planning_excludes_remediation_enabled=True)
    ids = [t.task_id for t in recent_planning_tasks(m, config)]
    assert ids == ["a", "c", "d"]


def _candidates(e: EvolutionEngine) -> list[str]:
    return list(e._gap_context_inputs(GAP).source_task_candidates)


def test_gap_candidates_flag_on_hide_lap_branch(mem_fs: dict[str, str]) -> None:
    e, m = _make(mem_fs, enabled=True)
    m.record_task(_rec("real1", "Real work", branch="maestro/task/real1"))
    m.record_task(_rec("lap1", "Remediate PR #7: fix", branch="maestro/evolve/p1"))
    assert _candidates(e) == ["real1 (maestro/task/real1): Real work"]


def test_gap_candidates_flag_off_render_lap(mem_fs: dict[str, str]) -> None:
    e, m = _make(mem_fs, enabled=False)
    m.record_task(_rec("lap1", "Remediate PR #7: fix", branch="maestro/evolve/p1"))
    assert _candidates(e) == ["lap1 (maestro/evolve/p1): Remediate PR #7: fix"]


def test_validate_source_task_id_refuses_lap_flag_on(mem_fs: dict[str, str]) -> None:
    e, m = _make(mem_fs, enabled=True)
    m.record_task(_rec("lap1", "Remediate PR #7: fix", branch="maestro/evolve/p1"))
    assert e._validate_source_task_id("lap1") is None


def test_validate_source_task_id_accepts_lap_flag_off(mem_fs: dict[str, str]) -> None:
    e, m = _make(mem_fs, enabled=False)
    m.record_task(_rec("lap1", "Remediate PR #7: fix", branch="maestro/evolve/p1"))
    assert e._validate_source_task_id("lap1") == "lap1"


def test_planning_base_branch_none_record() -> None:
    assert planning_base_branch(None, Config()) is None


def test_planning_base_branch_refuses_lap_flag_on() -> None:
    lap = _rec("lap1", "Remediate PR #7: fix", branch="maestro/evolve/p1")
    config = Config(planning_excludes_remediation_enabled=True)
    assert planning_base_branch(lap, config) is None


def test_planning_base_branch_keeps_lap_flag_off() -> None:
    lap = _rec("lap1", "Remediate PR #7: fix", branch="maestro/evolve/p1")
    assert planning_base_branch(lap, Config()) == "maestro/evolve/p1"


def test_planning_base_branch_keeps_real_task_flag_on() -> None:
    rec = _rec("t1", "real work", branch="maestro/task/t1")
    config = Config(planning_excludes_remediation_enabled=True)
    assert planning_base_branch(rec, config) == "maestro/task/t1"
