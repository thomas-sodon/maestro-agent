"""Tests for EvolutionEngine PR helpers — _pr_number_from_url and check_merged_plans."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from tests.support.merge_truth import merge_oracle, merge_truth

FAKE_DIR = Path("/fake/evoci")


def _make(
    mem_fs: dict[str, str],
    vcs: MagicMock | None = None,
    host: MagicMock | None = None,
    **config_kw: object,
) -> EvolutionEngine:
    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    cfg = Config(**config_kw)  # type: ignore[arg-type]
    return EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo", config=cfg,
        vcs=vcs, code_host=host,
    )


def _approved_plan(**overrides: object) -> EvolutionPlan:
    defaults: dict[str, object] = {
        "plan_id": "abc123",
        "title": "Test evolution",
        "category": "capability",
        "rationale": "For testing",
        "proposed_changes": ["Change X"],
        "files_affected": ["maestro/agent.py"],
        "expected_impact": "Improvement",
        "priority": "capability",
        "status": "approved",
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return EvolutionPlan(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# _pr_number_from_url
# ------------------------------------------------------------------

class TestPrNumberFromUrl:
    def test_standard_url(self) -> None:
        assert EvolutionEngine._pr_number_from_url(
            "https://github.com/owner/repo/pull/42"
        ) == 42

    def test_trailing_slash(self) -> None:
        assert EvolutionEngine._pr_number_from_url(
            "https://github.com/owner/repo/pull/99/"
        ) == 99

    def test_large_number(self) -> None:
        assert EvolutionEngine._pr_number_from_url(
            "https://github.com/org/project/pull/12345"
        ) == 12345

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError):
            EvolutionEngine._pr_number_from_url("")

    def test_non_numeric_suffix(self) -> None:
        with pytest.raises(ValueError):
            EvolutionEngine._pr_number_from_url(
                "https://github.com/o/r/pull/abc"
            )


# ------------------------------------------------------------------
# check_merged_plans
# ------------------------------------------------------------------

class TestCheckMergedPlans:
    def test_detects_merged_pr(self, mem_fs: dict[str, str]) -> None:
        host = MagicMock()
        e = _make(mem_fs, host=host)
        plan = _approved_plan(
            plan_id="merge01", status="implemented",
            pr_url="https://github.com/o/r/pull/10",
        )
        e._memory.save_evolution_plan(plan)

        result = e.check_merged_plans(
            settled_oracle=merge_oracle(
                {10: merge_truth(
                    10, settled=True, branch_landed=True, state="MERGED",
                )},
            ),
        )

        assert result.merged == ["merge01"]
        # The decision is sourced from the chokepoint oracle, not a raw host read.
        host.is_pr_merged.assert_not_called()
        updated = e._memory.get_plan("merge01")
        assert updated is not None
        assert updated.status == "merged"

    def test_skips_non_implemented_plans(self, mem_fs: dict[str, str]) -> None:
        e = _make(mem_fs, host=MagicMock())
        plan = _approved_plan(
            plan_id="skip01", status="approved",
            pr_url="https://github.com/o/r/pull/5",
        )
        e._memory.save_evolution_plan(plan)
        oracle = MagicMock()

        result = e.check_merged_plans(settled_oracle=oracle)

        assert result.merged == []
        oracle.assert_not_called()

    def test_skips_plans_without_pr_url(self, mem_fs: dict[str, str]) -> None:
        e = _make(mem_fs, host=MagicMock())
        plan = _approved_plan(plan_id="nourl01", status="implemented")
        e._memory.save_evolution_plan(plan)
        oracle = MagicMock()

        result = e.check_merged_plans(settled_oracle=oracle)

        assert result.merged == []
        oracle.assert_not_called()

    def test_not_yet_merged(self, mem_fs: dict[str, str]) -> None:
        e = _make(mem_fs, host=MagicMock())
        plan = _approved_plan(
            plan_id="open01", status="implemented",
            pr_url="https://github.com/o/r/pull/20",
        )
        e._memory.save_evolution_plan(plan)

        result = e.check_merged_plans(settled_oracle=merge_oracle({}))

        assert result.merged == []
        assert result.rejected == []
        updated = e._memory.get_plan("open01")
        assert updated is not None
        assert updated.status == "implemented"

    def test_malformed_pr_url_skipped(self, mem_fs: dict[str, str]) -> None:
        e = _make(mem_fs, host=MagicMock())
        plan = _approved_plan(
            plan_id="bad01", status="implemented",
            pr_url="not-a-url",
        )
        e._memory.save_evolution_plan(plan)
        oracle = MagicMock()

        result = e.check_merged_plans(settled_oracle=oracle)

        assert result.merged == []
        oracle.assert_not_called()

    def test_host_error_handled(self, mem_fs: dict[str, str]) -> None:
        e = _make(mem_fs, host=MagicMock())
        plan = _approved_plan(
            plan_id="err01", status="implemented",
            pr_url="https://github.com/o/r/pull/30",
        )
        e._memory.save_evolution_plan(plan)
        oracle = MagicMock(side_effect=RuntimeError("network error"))

        result = e.check_merged_plans(settled_oracle=oracle)

        assert result.merged == []


# ------------------------------------------------------------------
# Merged from bug hunt files — _pr_number_from_url edge cases
# ------------------------------------------------------------------


class TestPrNumberFromUrlEdgeCases:
    def test_none_input_raises_value_error(self) -> None:
        """None should raise ValueError, not AttributeError."""
        with pytest.raises(ValueError):
            EvolutionEngine._pr_number_from_url(None)  # type: ignore[arg-type]

    def test_zero_pr_number_is_invalid(self) -> None:
        """PR number 0 is invalid on GitHub."""
        with pytest.raises(ValueError, match="Invalid PR number"):
            EvolutionEngine._pr_number_from_url(
                "https://github.com/owner/repo/pull/0"
            )

    def test_negative_pr_number(self) -> None:
        with pytest.raises(ValueError):
            EvolutionEngine._pr_number_from_url(
                "https://github.com/owner/repo/pull/-1"
            )

    def test_pr_url_with_query_string(self) -> None:
        """_pr_number_from_url should handle query strings."""
        number = EvolutionEngine._pr_number_from_url(
            "https://github.com/owner/repo/pull/42?diff=unified"
        )
        assert number == 42

    def test_pr_url_with_fragment(self) -> None:
        """_pr_number_from_url should handle URL fragments."""
        number = EvolutionEngine._pr_number_from_url(
            "https://github.com/owner/repo/pull/99#issuecomment-123"
        )
        assert number == 99
