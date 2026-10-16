"""Guards for the submission-door split out of ``maestro/evolution.py`` (proposal 147)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_submission
from maestro.evolution import EvolutionEngine
from maestro.journal import Journal
from maestro.memory import _OBSOLETE_REASON_MAX_CHARS, EvolutionPlan, Memory
from maestro.source_budget import SourceBand, classify_band
from tests.test_evolution import _cli_plan, _make

_EVOLUTION_PY = Path(__file__).resolve().parent.parent / "maestro" / "evolution.py"


def test_evolution_py_stays_below_split_due() -> None:
    size = len(_EVOLUTION_PY.read_bytes())
    assert classify_band(size) == SourceBand.OK


@pytest.mark.asyncio
@pytest.mark.parametrize("self_initiated", [False, True])
async def test_submit_for_review_delegates_to_door(self_initiated: bool) -> None:
    engine = MagicMock(spec=EvolutionEngine)
    plan = MagicMock()
    with patch.object(
        evolution_submission, "submit_for_review", new_callable=AsyncMock,
    ) as spy:
        await EvolutionEngine.submit_for_review(
            engine, plan, self_initiated=self_initiated,
        )
    spy.assert_awaited_once_with(
        engine, plan, self_initiated=self_initiated,
        exempt_saturation=False,
    )


# ── Bounded submission-door refusal reasons (#4569) ──────────────────────────

_MANY_TOKENS = {f"measure-very-long-phantom-sensor-name-{i:02d}" for i in range(10)}


def _plain_plan(plan_id: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title="Bound refusal reasons",
        category="capability", rationale="keep refusal reasons short",
        proposed_changes=["tighten the reason"], files_affected=["maestro/x.py"],
        expected_impact="", created_at="2025-01-01T00:00:00Z",
    )


def _assert_bounded_obsolete(m: Memory, plan_id: str, prefix: str) -> None:
    stored = m.get_plan(plan_id)
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert len(stored.obsolete_reason) <= _OBSOLETE_REASON_MAX_CHARS
    assert stored.obsolete_reason.startswith(prefix)


def _journal_text(j: Journal) -> str:
    return " ".join(str(ev.content) for ev in j.read_events_by_type("evolution_note"))


@pytest.mark.asyncio
async def test_unshipped_cli_evidence_many_tokens_records_obsolete_bounded(
    mem_fs: dict[str, str],
) -> None:
    e, m, _c, j = _make(mem_fs)
    notify = AsyncMock()
    with (
        patch.object(e._notifier, "notify_plan_for_review", notify),
        patch.object(
            evolution_submission, "_evidence_cli_grounding_verdict",
            return_value=set(_MANY_TOKENS),
        ),
    ):
        await e.submit_for_review(_plain_plan("ev-many"))
    _assert_bounded_obsolete(m, "ev-many", "Cites unshipped CLI evidence")
    notify.assert_not_awaited()
    text = _journal_text(j)
    for token in _MANY_TOKENS:
        assert token in text


@pytest.mark.asyncio
async def test_shipped_cli_many_tokens_records_obsolete_bounded(
    mem_fs: dict[str, str],
) -> None:
    e, m, _c, j = _make(mem_fs)
    with patch.object(
        evolution_submission, "_cli_grounding_verdict",
        return_value=set(_MANY_TOKENS),
    ):
        await e.submit_for_review(
            _cli_plan("cli-many", "Add CLI", ["Register new subcommands"]),
        )
    _assert_bounded_obsolete(
        m, "cli-many", "Proposes only already-shipped CLI surface",
    )
    text = _journal_text(j)
    for token in _MANY_TOKENS:
        assert token in text


@pytest.mark.asyncio
async def test_theme_cluster_many_plans_records_obsolete_bounded(
    mem_fs: dict[str, str],
) -> None:
    e, m, _c, j = _make(mem_fs)
    cluster = [_plain_plan(f"cluster{i:05d}") for i in range(20)]
    with patch.object(
        e._memory, "find_theme_cluster_pending_plans", return_value=cluster,
    ):
        await e.submit_for_review(_plain_plan("theme-many"))
    _assert_bounded_obsolete(
        m, "theme-many", "Themed restatement of 20 live plans",
    )
    text = _journal_text(j)
    for p in cluster:
        assert p.plan_id in text


@pytest.mark.parametrize(
    "reason", ["x" * (_OBSOLETE_REASON_MAX_CHARS + 1), "   "],
)
def test_record_obsolete_duplicate_rejects_bad_reason_before_save(
    mem_fs: dict[str, str], reason: str,
) -> None:
    e, m, _c, _j = _make(mem_fs)
    plan = _plain_plan("bad-reason")
    with pytest.raises(ValueError, match="reason"):
        evolution_submission._record_obsolete_duplicate(
            e, plan, obsolete_reason=reason, journal_content="j",
            triggered_by="t",
        )
    assert m.get_plan("bad-reason") is None
    assert all(p.plan_id != "bad-reason" for p in m.get_pending_plans())


def test_bounded_reason_list_under_budget_verbatim() -> None:
    assert evolution_submission._bounded_reason_list(["a", "bb"], 20) == "a, bb"


def test_bounded_reason_list_truncates_with_more_suffix() -> None:
    got = evolution_submission._bounded_reason_list(["aaaa", "bbbb", "cccc"], 10)
    assert got == "aaaa, bbbb +1 more"


def test_bounded_reason_list_clips_lone_overbudget_item() -> None:
    assert evolution_submission._bounded_reason_list(["abcdefgh"], 4) == "abc\u2026"


def test_bounded_reason_list_clips_first_item_and_counts_rest() -> None:
    got = evolution_submission._bounded_reason_list(["abcdefgh", "z"], 4)
    assert got == "abc\u2026 +1 more"


def test_bounded_reason_list_empty() -> None:
    assert evolution_submission._bounded_reason_list([], 10) == ""
