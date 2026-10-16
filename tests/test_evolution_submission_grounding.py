"""Submission-door premise grounding, admission backstop and rejection cooldown
(``maestro/evolution_submission.py``); split out of
``tests/test_evolution.py``.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    _GROUNDING_LOCATOR_MAX_CHARS,
    _LINT_PROBE_TIMEOUT_SECONDS,
    EvolutionEngine,
)
from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    EvolutionPlan,
    Memory,
)
from maestro.plan_grounding import GroundingProbe
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import GAP
from tests.test_evolution import (
    _SIMILAR_RATIONALE,
    _engine_with,
    _make,
    _make_engine,
    _RecordingGateBackend,
    _themed_candidate,
    _themed_obsolete,
)


@pytest.mark.asyncio
async def test_propose_plan_grounding_probe_is_flag_gated(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: probe emission is gated behind ``plan_grounding_enabled``.

    Flag off → the propose_plan prompt carries no grounding instruction, the
    ``output_format`` schema has no ``grounding`` property, and a reply carrying a
    grounding object still parses ``grounding=None``. Flag on → the instruction
    and the schema property appear and the reply's probe is parsed onto the plan.
    """
    plan_json = json.dumps({
        "title": "Ground it", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": ["x"],
        "grounding": {
            "kind": "SYMBOL_ABSENT", "locator": "brand_new_gate", "expectation": "",
        },
    })

    # Flag OFF — byte-identical to today: no instruction, no schema property, and
    # the returned plan carries no probe even though the reply included one.
    e_off, _m, _c, _j = _make(mem_fs)
    e_off._config = replace(e_off._config, plan_grounding_enabled=False)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_off:
        mock_off.return_value = MockQueryResult(response=plan_json)
        plan_off = await e_off.propose_plan(gap=GAP)
    prompt_off = str(mock_off.call_args.kwargs["prompt"])
    schema_off = mock_off.call_args.kwargs["options"].output_format["schema"]
    assert "PLAN-PREMISE GROUNDING" not in prompt_off
    assert "grounding" not in schema_off["properties"]
    assert plan_off.grounding is None

    # Flag ON — the instruction and the schema property appear, and the
    # defect-framed reply's probe is parsed onto the plan.
    e_on, _m2, _c2, _j2 = _make(mem_fs)
    e_on._config = replace(e_on._config, plan_grounding_enabled=True)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_on:
        mock_on.return_value = MockQueryResult(response=plan_json)
        plan_on = await e_on.propose_plan(gap=GAP)
    prompt_on = str(mock_on.call_args.kwargs["prompt"])
    schema_on = mock_on.call_args.kwargs["options"].output_format["schema"]
    assert "PLAN-PREMISE GROUNDING" in prompt_on
    assert "grounding" in schema_on["properties"]
    assert plan_on.grounding is not None
    assert plan_on.grounding.kind == "SYMBOL_ABSENT"
    assert plan_on.grounding.locator == "brand_new_gate"


@pytest.mark.asyncio
async def test_propose_plan_workspace_citation_is_moment_gated(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 130: plan-proposal citation is gated on a broadcast moment.

    No moment → the prompt carries no citation instruction, the schema has no
    ``workspace_item_ids`` property, and the returned plan's citation fields are
    ``None`` even if the reply declares ids. A moment present → the instruction
    and property appear, the moment block is prepended, the moment id is stamped,
    a declared *admitted* id lands on the plan, and a declared *non-admitted*
    (hallucinated) id is dropped by the in-producer resolve filter. When a moment
    is present but the reply declares no ids, ``workspace_item_ids`` is ``[]``
    (saw the moment, cited nothing) — distinct from ``None``.
    """
    plan_json = json.dumps({
        "title": "Cite it", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": ["x"],
        "workspace_item_ids": ["wi-a", "wi-bogus"],
    })

    # No moment — byte-identical to today: no instruction, no schema property,
    # citation fields None even though the reply declared ids.
    e_off, _m, _c, _j = _make(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_off:
        mock_off.return_value = MockQueryResult(response=plan_json)
        plan_off = await e_off.propose_plan(gap=GAP)
    prompt_off = str(mock_off.call_args.kwargs["prompt"])
    schema_off = mock_off.call_args.kwargs["options"].output_format["schema"]
    assert "CONSCIOUS-ACCESS CITATION" not in prompt_off
    assert "workspace_item_ids" not in schema_off["properties"]
    assert plan_off.workspace_moment_id is None
    assert plan_off.workspace_item_ids is None

    # Moment present — instruction + property appear, the block is prepended, the
    # moment id is stamped, the admitted id survives, the hallucinated id is
    # filtered out by the in-producer resolve.
    e_on, _m2, _c2, _j2 = _make(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_on:
        mock_on.return_value = MockQueryResult(response=plan_json)
        plan_on = await e_on.propose_plan(
            gap=GAP,
            workspace_moment="## Conscious moment\n- wi-a [audit/x] a summary",
            workspace_moment_id="cm-1",
            admitted_item_ids=frozenset({"wi-a"}),
        )
    prompt_on = str(mock_on.call_args.kwargs["prompt"])
    schema_on = mock_on.call_args.kwargs["options"].output_format["schema"]
    assert "CONSCIOUS-ACCESS CITATION" in prompt_on
    assert prompt_on.startswith("## Conscious moment")
    assert "workspace_item_ids" in schema_on["properties"]
    assert plan_on.workspace_moment_id == "cm-1"
    assert plan_on.workspace_item_ids == ["wi-a"]

    # Moment present but the reply declares no ids → [] (distinct from None).
    plan_json_no_ids = json.dumps({
        "title": "Cite nothing", "category": "capability",
        "priority": "capability", "rationale": "test",
        "proposed_changes": ["change"], "files_affected": [],
        "expected_impact": "", "acceptance_criteria": ["x"],
    })
    e_none, _m3, _c3, _j3 = _make(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_none:
        mock_none.return_value = MockQueryResult(response=plan_json_no_ids)
        plan_none = await e_none.propose_plan(
            gap=GAP,
            workspace_moment="## Conscious moment\n- wi-a [audit/x] a summary",
            workspace_moment_id="cm-2",
            admitted_item_ids=frozenset({"wi-a"}),
        )
    assert plan_none.workspace_moment_id == "cm-2"
    assert plan_none.workspace_item_ids == []


@pytest.mark.asyncio
async def test_propose_plan_workspace_citation_dedups_declared_ids(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 130: a reply repeating an admitted id persists exactly one copy.

    The in-producer resolve filter is order-preserving *and* de-duplicating
    (mirroring the finalized episode's ``cited_plan_ids``), so a model reply naming
    ``wi-a`` three times and ``wi-b`` once lands ``["wi-a", "wi-b"]`` on the plan —
    never the four-element list with duplicates the pre-de-dup filter would keep.
    """
    plan_json = json.dumps({
        "title": "Cite it thrice", "category": "capability",
        "priority": "capability", "rationale": "test",
        "proposed_changes": ["change"], "files_affected": [],
        "expected_impact": "", "acceptance_criteria": ["x"],
        "workspace_item_ids": ["wi-a", "wi-a", "wi-b", "wi-a"],
    })
    e, _m, _c, _j = _make(mem_fs)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(
            gap=GAP,
            workspace_moment=(
                "## Conscious moment\n- wi-a [audit/x] a\n- wi-b [audit/x] b"
            ),
            workspace_moment_id="cm-dup",
            admitted_item_ids=frozenset({"wi-a", "wi-b"}),
        )
    assert plan.workspace_item_ids == ["wi-a", "wi-b"]


@pytest.mark.asyncio
async def test_submit_for_review_obsoletes_ungrounded_symbol_absent_plan(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a plan whose SYMBOL_ABSENT locator already exists is refused.

    With ``plan_grounding_enabled`` on, submit_for_review confirms the probe
    against the injected tree text as its first guard: a locator already defined
    in the corpus is ``ungrounded("duplicate")``, so the plan is recorded obsolete
    with an ``[ungrounded:duplicate]`` reason, a ``plan_grounding`` gate-activation
    row is stamped, and the human is never notified.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    # Inject a corpus that already defines the "new" symbol → duplicate.
    e._read_grounding_tree_text = (
        lambda probe: "def already_live_gate(x):\n    return x\n"
    )

    plan = EvolutionPlan(
        plan_id="ungrounded1", title="Build already_live_gate",
        category="capability",
        rationale="Add a new gate", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="SYMBOL_ABSENT", locator="already_live_gate", expectation="",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(plan)

    stored = m.get_plan("ungrounded1")
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert "[ungrounded:duplicate]" in stored.obsolete_reason
    e._notifier.notify_plan_for_review.assert_not_awaited()
    assert len(gates.rows) == 1
    row = gates.rows[0]
    assert row.gate == "plan_grounding"
    assert row.action == "obsolete"
    assert "[ungrounded:duplicate]" in row.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(("grounded_reads", "suffix"), [
    (["./maestro/target.py"], " [planner-read]"),
    ([], " [planner-unread]"),
    (None, ""),
])
async def test_submit_for_review_ungrounded_reason_carries_planner_read_tag(
    mem_fs: dict[str, str], grounded_reads: list[str] | None, suffix: str,
) -> None:
    """Proposal 143 step 5: the refusal records whether the planner read the locator.

    The ``[ungrounded:<category>]`` bracket stays leading so
    ``_UNGROUNDED_REASON_RE`` still yields the bare category, the verdict and the
    gate-activation detail are unchanged, and a blind planner (``None``) keeps
    today's exact reason.
    """
    from maestro.audit_checks import _UNGROUNDED_REASON_RE

    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    e._read_grounding_tree_text = lambda probe: "def unrelated():\n    pass\n"

    plan = EvolutionPlan(
        plan_id="tagged1", title="Fix the defect",
        category="capability",
        rationale="r", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="DEFECT_PRESENT", locator="maestro/target.py",
            expectation="broken_needle",
        ),
        grounded_reads=grounded_reads,
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(plan)

    stored = m.get_plan("tagged1")
    assert stored is not None
    assert stored.status == "obsolete"
    expected = (
        "[ungrounded:no-such-defect] grounding probe DEFECT_PRESENT on "
        "maestro/target.py is refuted by a fresh reading of the tree" + suffix
    )
    assert stored.obsolete_reason == expected
    match = _UNGROUNDED_REASON_RE.match(expected)
    assert match is not None
    assert match.group(1) == "no-such-defect"
    e._notifier.notify_plan_for_review.assert_not_awaited()
    assert gates.rows[0].detail == "[ungrounded:no-such-defect] DEFECT_PRESENT maestro/target.py"


@pytest.mark.asyncio
async def test_submit_for_review_obsoletes_ungrounded_behavior_absent_plan(
    mem_fs: dict[str, str],
) -> None:
    """A BEHAVIOR_ABSENT plan whose witness already ships is refused as a duplicate.

    The behavioral inverse of the SYMBOL_ABSENT case: the plan proposes a fresh
    name, but the witness substring is already present in the file the behavior
    would live in, so the mechanism already ships under some other name. The plan
    is recorded obsolete with ``[ungrounded:duplicate]``, one ``plan_grounding``
    gate row is stamped, and the human is never notified.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    # The named file already contains the witness → the behavior already ships.
    e._read_grounding_tree_text = (
        lambda probe: "def _gate_pr_remediation_settled(self, item):\n"
        "    if self._is_pr_settled(item.pr_number):\n"
        "        return None\n"
    )

    plan = EvolutionPlan(
        plan_id="ungroundedb1", title="Build a settled-PR dequeue gate",
        category="capability",
        rationale="Nothing drops a settled PR from the queue", proposed_changes=["c"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="BEHAVIOR_ABSENT", locator="maestro/loop/dequeue.py",
            expectation="self._is_pr_settled(",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(plan)

    stored = m.get_plan("ungroundedb1")
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert "[ungrounded:duplicate]" in stored.obsolete_reason
    assert "BEHAVIOR_ABSENT" in stored.obsolete_reason
    e._notifier.notify_plan_for_review.assert_not_awaited()
    assert len(gates.rows) == 1
    assert gates.rows[0].gate == "plan_grounding"
    assert gates.rows[0].action == "obsolete"


@pytest.mark.asyncio
async def test_submit_for_review_ungrounded_overlong_locator_records_obsolete(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: an over-length model locator can't crash the grounding guard.

    A model-supplied ``locator`` longer than the obsolete-reason budget (~176
    chars) once flowed uncapped into the ``[ungrounded:<cat>]`` reason string,
    which ``Memory.mark_plan_obsolete`` rejects above ``_OBSOLETE_REASON_MAX_CHARS``
    (280). Because ``_record_obsolete_duplicate`` saves the plan *before* marking
    it obsolete, the raise would leave the ungrounded plan persisted
    ``pending_review`` (reachable by the reviewer) and crash submission — defeating
    the guard. ``_grounding_from_spec`` now caps the locator, so the plan is
    recorded ``obsolete`` with a bounded reason and no raise.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates

    # A locator well over the reason budget, parsed through the capping seam.
    overlong = "z" * (_OBSOLETE_REASON_MAX_CHARS + 40)
    probe = e._grounding_from_spec(
        {"grounding": {"kind": "SYMBOL_ABSENT", "locator": overlong}},
    )
    assert probe is not None
    assert len(probe.locator) == _GROUNDING_LOCATOR_MAX_CHARS
    # Corpus defines the (capped) symbol → ungrounded("duplicate").
    e._read_grounding_tree_text = (
        lambda p: f"def {p.locator}(x):\n    return x\n"
    )

    plan = EvolutionPlan(
        plan_id="ungrounded_long1", title="Build an over-named gate",
        category="capability",
        rationale="Add a new gate", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=probe,
        created_at="2025-01-01T00:00:00Z",
    )
    # No raise despite the over-length model locator.
    await e.submit_for_review(plan)

    stored = m.get_plan("ungrounded_long1")
    assert stored is not None
    # Recorded obsolete, NOT left reachable as pending_review.
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert "[ungrounded:duplicate]" in stored.obsolete_reason
    assert len(stored.obsolete_reason) <= _OBSOLETE_REASON_MAX_CHARS
    e._notifier.notify_plan_for_review.assert_not_awaited()
    assert len(gates.rows) == 1


@pytest.mark.asyncio
async def test_submit_for_review_grounded_plan_falls_through_to_guards(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a grounded probe lets the plan reach the normal guards.

    A SYMBOL_ABSENT locator NOT defined in the injected corpus is ``grounded`` —
    the guard falls through, the plan is saved ``pending_review`` and the human is
    notified, and no gate-activation row is stamped.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    e._read_grounding_tree_text = lambda probe: "# corpus without the symbol\n"

    plan = EvolutionPlan(
        plan_id="grounded1", title="Build genuinely_new_gate",
        category="capability",
        rationale="Add a new gate", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="SYMBOL_ABSENT", locator="genuinely_new_gate", expectation="",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(plan)

    stored = m.get_plan("grounded1")
    assert stored is not None
    assert stored.status == "pending_review"
    e._notifier.notify_plan_for_review.assert_awaited_once()
    # The review buttons bind to the snapshot the message renders (#4146).
    assert e._notifier.notify_plan_for_review.await_args.args[2] == (
        stored.plan_content_hash
    )
    assert gates.rows == []


@pytest.mark.asyncio
async def test_submit_for_review_skips_grounding_when_disabled_or_no_probe(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: the tree-read seam is untouched flag-off or with no probe.

    Neither the flag-off path (probe present, flag off) nor a flag-on plan
    carrying no probe touches ``_read_grounding_tree_text`` — both reach the normal
    guards, are saved ``pending_review``, and notify the human.
    """
    # (a) Flag off, probe present → guard skipped, tree never read.
    e_off, m_off, _c, _j = _make(mem_fs)
    e_off._config = replace(e_off._config, plan_grounding_enabled=False)
    e_off._notifier.notify_plan_for_review = AsyncMock()
    reads_off: list[str] = []

    def _spy_off(probe: GroundingProbe) -> str:
        reads_off.append(probe.locator)
        return "def already_live_gate(x):\n    return x\n"

    e_off._read_grounding_tree_text = _spy_off
    plan_off = EvolutionPlan(
        plan_id="flagoff1", title="Build already_live_gate off",
        category="capability",
        rationale="Add a gate", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="SYMBOL_ABSENT", locator="already_live_gate", expectation="",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    await e_off.submit_for_review(plan_off)
    assert reads_off == []  # tree never read with the flag off
    stored_off = m_off.get_plan("flagoff1")
    assert stored_off is not None
    assert stored_off.status == "pending_review"
    e_off._notifier.notify_plan_for_review.assert_awaited_once()

    # (b) Flag on, no probe → guard skipped, tree never read.
    e_on, m_on, _c2, _j2 = _make(mem_fs)
    e_on._config = replace(e_on._config, plan_grounding_enabled=True)
    e_on._notifier.notify_plan_for_review = AsyncMock()
    reads_on: list[str] = []

    def _spy_on(probe: GroundingProbe) -> str:
        reads_on.append(probe.locator)
        return ""

    e_on._read_grounding_tree_text = _spy_on
    plan_none = EvolutionPlan(
        plan_id="noprobe1", title="Open-ended feature work",
        category="capability",
        rationale="Explore", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=None,
        created_at="2025-01-01T00:00:00Z",
    )
    await e_on.submit_for_review(plan_none)
    assert reads_on == []
    stored_none = m_on.get_plan("noprobe1")
    assert stored_none is not None
    assert stored_none.status == "pending_review"
    e_on._notifier.notify_plan_for_review.assert_awaited_once()


def test_grounding_from_spec_parses_and_rejects(mem_fs: dict[str, str]) -> None:
    """Proposal 114: _grounding_from_spec parses a valid probe, rejects the rest.

    Covers the valid parse (with a non-string expectation coerced to ``""`` and a
    trimmed locator), every malformed-input rejection branch, and the flag-off
    short-circuit.
    """
    e = _make_engine(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)

    probe = e._grounding_from_spec({
        "grounding": {
            "kind": "SYMBOL_ABSENT", "locator": "  new_gate  ", "expectation": 5,
        },
    })
    assert probe is not None
    assert probe.kind == "SYMBOL_ABSENT"
    assert probe.locator == "new_gate"
    assert probe.expectation == ""

    # A model-supplied locator longer than the cap is truncated at parse time so
    # it can never overflow the obsolete-reason ceiling in the submission guard.
    long_probe = e._grounding_from_spec({
        "grounding": {
            "kind": "DEFECT_PRESENT",
            "locator": "x" * (_GROUNDING_LOCATOR_MAX_CHARS + 80),
            "expectation": "needle",
        },
    })
    assert long_probe is not None
    assert len(long_probe.locator) == _GROUNDING_LOCATOR_MAX_CHARS

    # BEHAVIOR_ABSENT parses like any other kind — the closed ``kind`` check
    # derives from ``get_args(GroundingKind)``, so the new kind needed no edit.
    behavior = e._grounding_from_spec({
        "grounding": {
            "kind": "BEHAVIOR_ABSENT",
            "locator": "  maestro/loop/dequeue.py  ",
            "expectation": "self._is_pr_settled(",
        },
    })
    assert behavior is not None
    assert behavior.kind == "BEHAVIOR_ABSENT"
    assert behavior.locator == "maestro/loop/dequeue.py"
    assert behavior.expectation == "self._is_pr_settled("

    # A witness-less BEHAVIOR_ABSENT probe is declined outright: with nothing to
    # refute the premise it could only ever ground, which is indistinguishable
    # from a real pass. Dropping it to None leaves the plan unguarded instead.
    assert e._grounding_from_spec({
        "grounding": {
            "kind": "BEHAVIOR_ABSENT",
            "locator": "maestro/loop/dequeue.py",
            "expectation": "   ",
        },
    }) is None
    assert e._grounding_from_spec({
        "grounding": {
            "kind": "BEHAVIOR_ABSENT", "locator": "maestro/loop/dequeue.py",
        },
    }) is None

    # Malformed / absent probes → None.
    assert e._grounding_from_spec({}) is None
    assert e._grounding_from_spec({"grounding": "not-a-dict"}) is None
    assert e._grounding_from_spec(
        {"grounding": {"kind": "BOGUS", "locator": "x"}},
    ) is None
    assert e._grounding_from_spec(
        {"grounding": {"kind": "SYMBOL_ABSENT", "locator": "   "}},
    ) is None

    # A locator carrying an embedded null byte (``chr(0)`` keeps the NUL out of
    # this source file) is NOT rejected at parse time — ``.strip()`` does not
    # drop NUL bytes and the result stays non-empty — so it survives to the
    # confined read, where the resulting ``ValueError: embedded null byte`` is
    # failed open (pinned in
    # ``test_read_grounding_tree_text_confines_defect_locator``).
    null_probe = e._grounding_from_spec({
        "grounding": {
            "kind": "DEFECT_PRESENT", "locator": "maestro/ev" + chr(0) + "il.py",
        },
    })
    assert null_probe is not None
    assert chr(0) in null_probe.locator

    # Flag off → always None, even for a well-formed probe.
    e._config = replace(e._config, plan_grounding_enabled=False)
    assert e._grounding_from_spec(
        {"grounding": {"kind": "SYMBOL_ABSENT", "locator": "x"}},
    ) is None


def test_read_grounding_tree_text_defect_and_symbol_corpus(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: the FS seam reads a defect file and the symbol corpus.

    Exercises the real ``_read_grounding_tree_text`` body (which the submission
    tests otherwise override): a ``DEFECT_PRESENT`` file read plus its fail-open,
    and a ``SYMBOL_ABSENT`` corpus scan that skips an unreadable member.
    """
    ws = "/fake/grounding_ws"
    e = _engine_with(replace(Config(), work_repo_workspace=ws))

    # DEFECT_PRESENT — reads the single named file at the work-target root.
    mem_fs[f"{ws}/maestro/target.py"] = "the-needle-is-here\n"
    defect = GroundingProbe(
        kind="DEFECT_PRESENT", locator="maestro/target.py",
        expectation="the-needle-is-here",
    )
    assert "the-needle-is-here" in e._read_grounding_tree_text(defect)

    # DEFECT_PRESENT — an unreadable path fails open to "".
    missing = GroundingProbe(
        kind="DEFECT_PRESENT", locator="maestro/no_such_file_xyz.py",
        expectation="x",
    )
    assert e._read_grounding_tree_text(missing) == ""

    # BEHAVIOR_ABSENT — reads the same single named file as DEFECT_PRESENT, but
    # an unreadable path fails open to ``None``, not "": an empty text would
    # ground the plan, so "" would make a broken locator indistinguishable from
    # a genuine "no witness here" pass.
    behavior = GroundingProbe(
        kind="BEHAVIOR_ABSENT", locator="maestro/target.py",
        expectation="the-needle-is-here",
    )
    read = e._read_grounding_tree_text(behavior)
    assert read is not None
    assert "the-needle-is-here" in read
    behavior_missing = GroundingProbe(
        kind="BEHAVIOR_ABSENT", locator="maestro/no_such_file_xyz.py",
        expectation="x",
    )
    assert e._read_grounding_tree_text(behavior_missing) is None

    # SYMBOL_ABSENT — concatenates the maestro corpus; a readable member is
    # included and an unreadable one is skipped (fail-open).
    present = Path(f"{ws}/maestro/present.py")
    absent = Path(f"{ws}/maestro/absent_xyz.py")  # not seeded, not on disk
    mem_fs[str(present)] = "def already_here():\n    return 1\n"
    symbol = GroundingProbe(
        kind="SYMBOL_ABSENT", locator="already_here", expectation="",
    )
    with patch.object(Path, "rglob", return_value=[present, absent]):
        corpus = e._read_grounding_tree_text(symbol)
    assert "def already_here()" in corpus


@pytest.mark.parametrize("kind", ["DEFECT_PRESENT", "BEHAVIOR_ABSENT"])
def test_read_grounding_tree_text_reads_data_locator(
    mem_fs: dict[str, str], kind: str,
) -> None:
    """Proposal 143: the door keeps today's confinement for a ``data/`` locator.

    The engine seam routes through the door reader, not the stricter tool
    reader whose deny set refuses ``data`` — so a submission verdict is unchanged.
    """
    ws = "/fake/grounding_ws"
    e = _engine_with(replace(Config(), work_repo_workspace=ws))
    mem_fs[f"{ws}/data/lifecycle.json"] = '{"stage": "needle"}'
    probe = GroundingProbe(
        kind=kind, locator="data/lifecycle.json", expectation="needle",
    )
    assert e._read_grounding_tree_text(probe) == '{"stage": "needle"}'


def test_read_grounding_tree_text_confines_defect_locator(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a DEFECT_PRESENT locator cannot escape the workspace.

    An absolute or ``..``-bearing model-supplied locator is rejected unread
    (yields ""), so a probe can never read a file outside the work-target tree,
    and a non-UTF-8 file is treated as the documented "" fail-open rather than
    crashing submission.
    """
    ws = "/fake/grounding_ws"
    e = _engine_with(replace(Config(), work_repo_workspace=ws))

    # An absolute locator is refused before any read — even a seeded target.
    mem_fs["/etc/secret_xyz"] = "TOP-SECRET\n"
    absolute = GroundingProbe(
        kind="DEFECT_PRESENT", locator="/etc/secret_xyz",
        expectation="TOP-SECRET",
    )
    assert e._read_grounding_tree_text(absolute) == ""

    # A ``..`` traversal locator is refused before any read.
    mem_fs["/fake/outside.py"] = "escaped\n"
    traversal = GroundingProbe(
        kind="DEFECT_PRESENT", locator="../outside.py", expectation="escaped",
    )
    assert e._read_grounding_tree_text(traversal) == ""

    # A BEHAVIOR_ABSENT locator is confined on exactly the same path, failing
    # open to ``None`` (never "" — see the read test above).
    behavior_absolute = GroundingProbe(
        kind="BEHAVIOR_ABSENT", locator="/etc/secret_xyz",
        expectation="TOP-SECRET",
    )
    assert e._read_grounding_tree_text(behavior_absolute) is None
    behavior_traversal = GroundingProbe(
        kind="BEHAVIOR_ABSENT", locator="../outside.py", expectation="escaped",
    )
    assert e._read_grounding_tree_text(behavior_traversal) is None

    # A non-UTF-8 file is treated as unreadable ("" fail-open), not a crash.
    binary = GroundingProbe(
        kind="DEFECT_PRESENT", locator="maestro/binary.py", expectation="x",
    )
    decode_error = UnicodeDecodeError(
        "utf-8", b"\xff", 0, 1, "invalid start byte",
    )
    with patch.object(Path, "read_text", side_effect=decode_error):
        assert e._read_grounding_tree_text(binary) == ""

    # A model-supplied locator carrying an embedded null byte (built via
    # ``chr(0)`` so no NUL lands in this source file) passes confinement
    # (a null byte is neither absolute nor a ``..`` component), so it
    # reaches the read; ``open()`` raises a plain ``ValueError: embedded
    # null byte`` -- not an ``OSError``/``UnicodeDecodeError`` -- which must
    # be caught and failed open to "" rather than crashing
    # ``submit_for_review``. That such a locator also survives
    # ``_grounding_from_spec`` is pinned in
    # ``test_grounding_from_spec_parses_and_rejects``.
    null_byte = GroundingProbe(
        kind="DEFECT_PRESENT",
        locator="maestro/ev" + chr(0) + "il.py",
        expectation="x",
    )
    with patch.object(
        Path, "read_text", side_effect=ValueError("embedded null byte"),
    ):
        assert e._read_grounding_tree_text(null_byte) == ""


def test_run_lint_probe_maps_ruff_exit_codes(mem_fs: dict[str, str]) -> None:
    """Proposal 114: ``_run_lint_probe`` maps ruff's exit codes, subprocess mocked.

    Exit 0 (clean) → ``""``, exit 1 (violation) → the findings text, exit >= 2
    (ruff itself errored, e.g. an unknown rule) → ``None`` (fail open), and a
    launch/timeout failure *or* a ``ValueError`` (the ``embedded null byte`` a
    model-supplied locator can smuggle past confinement) → ``None`` rather than
    escaping to crash ``submit_for_review``. ``maestro.evolution.subprocess.run``
    is patched so no real process is spawned.
    """
    e = _make_engine(mem_fs)

    # Exit 0 — ruff ran clean, no finding → "".
    clean = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("maestro.evolution.subprocess.run", return_value=clean) as run0:
        assert e._run_lint_probe("RUF036", "maestro/target.py") == ""
    # The bounded invocation shells out to ``python -m ruff check --select`` with
    # ``check=False`` and the module timeout — mirroring ``run_gate_subprocess``.
    argv = run0.call_args.args[0]
    assert argv[1:4] == ["-m", "ruff", "check"]
    assert "--select" in argv
    assert "RUF036" in argv
    assert "maestro/target.py" in argv
    assert run0.call_args.kwargs["check"] is False
    assert run0.call_args.kwargs["timeout"] == _LINT_PROBE_TIMEOUT_SECONDS

    # Exit 1 — ruff reported the rule; the captured findings text is returned.
    findings = "maestro/target.py:12:5: RUF036 error\n"
    violation = subprocess.CompletedProcess(
        args=[], returncode=1, stdout=findings, stderr="",
    )
    with patch("maestro.evolution.subprocess.run", return_value=violation):
        assert e._run_lint_probe("RUF036", "maestro/target.py") == findings

    # Exit 2 — ruff itself errored (e.g. an unknown rule) → None (fail open).
    errored = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="", stderr="error: unknown rule\n",
    )
    with patch("maestro.evolution.subprocess.run", return_value=errored):
        assert e._run_lint_probe("BOGUS9", "maestro/target.py") is None

    # A timeout (or launch failure) → None (fail open).
    with patch(
        "maestro.evolution.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ruff", timeout=1),
    ):
        assert e._run_lint_probe("RUF036", "maestro/target.py") is None

    # A ``ValueError`` (``embedded null byte`` in the path) → None (fail open),
    # not an escaping crash. ``subprocess.run`` raises this as a plain
    # ``ValueError``, outside ``OSError``/``SubprocessError``, so it must be in
    # the caught tuple.
    with patch(
        "maestro.evolution.subprocess.run",
        side_effect=ValueError("embedded null byte"),
    ):
        assert e._run_lint_probe("RUF036", "maestro/t\x00.py") is None


def test_read_grounding_tree_text_lint_null_byte_locator_fails_open(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a null-byte LINT_PRESENT locator fails open, never crashes.

    A locator carrying an embedded null byte is neither absolute nor a ``..``
    component, so it passes the confinement check and reaches the real
    ``_run_lint_probe``, where ``subprocess.run`` raises
    ``ValueError('embedded null byte')``. That ``ValueError`` is caught (fail
    open) so the seam returns ``None`` rather than escaping to crash the async
    ``submit_for_review`` path — the exact regression the caught-tuple fix
    guards. ``subprocess.run`` is patched so no real process is spawned.
    """
    e = _make_engine(mem_fs)
    null_byte = GroundingProbe(
        kind="LINT_PRESENT", locator="maestro/t\x00.py", expectation="RUF036",
    )
    with patch(
        "maestro.evolution.subprocess.run",
        side_effect=ValueError("embedded null byte"),
    ):
        assert e._read_grounding_tree_text(null_byte) is None


def test_read_grounding_tree_text_lint_present_routes_and_confines(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: the FS seam routes LINT_PRESENT to the ruff reader, confined.

    A confined ``LINT_PRESENT`` locator delegates to ``_run_lint_probe`` (overridden
    here so no subprocess runs), passing the rule from ``expectation`` and the path
    from ``locator``. An absolute or ``..`` locator is rejected unread and fails
    **open** to ``None`` (not ``""``) so an unconfinable lint probe never
    false-refuses a plan.
    """
    e = _make_engine(mem_fs)
    calls: list[tuple[str, str]] = []

    def _fake_probe(rule: str, path: str) -> str | None:
        calls.append((rule, path))
        return "maestro/t.py:1:1: RUF036 finding\n"

    e._run_lint_probe = _fake_probe

    lint = GroundingProbe(
        kind="LINT_PRESENT", locator="maestro/t.py", expectation="RUF036",
    )
    assert e._read_grounding_tree_text(lint) == "maestro/t.py:1:1: RUF036 finding\n"
    assert calls == [("RUF036", "maestro/t.py")]

    # An absolute locator is refused before any read → None (fail open).
    absolute = GroundingProbe(
        kind="LINT_PRESENT", locator="/etc/passwd", expectation="RUF036",
    )
    assert e._read_grounding_tree_text(absolute) is None

    # A ``..`` traversal locator is refused before any read → None (fail open).
    traversal = GroundingProbe(
        kind="LINT_PRESENT", locator="../outside.py", expectation="RUF036",
    )
    assert e._read_grounding_tree_text(traversal) is None

    # Only the confined read reached the ruff reader.
    assert calls == [("RUF036", "maestro/t.py")]


@pytest.mark.asyncio
async def test_submit_for_review_obsoletes_ungrounded_lint_present_plan(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a LINT_PRESENT plan whose ruff rule runs clean is refused.

    The reader returns ``""`` (ruff clean) → ``confirm`` is ``no-such-lint`` → the
    plan is recorded obsolete with ``[ungrounded:no-such-lint]``, a
    ``plan_grounding`` gate-activation row is stamped, and the human is never
    notified — the hallucinated-lint-error case the probe exists to catch.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    # Ruff ran clean → "" → no-such-lint.
    e._read_grounding_tree_text = lambda probe: ""

    plan = EvolutionPlan(
        plan_id="lint_clean1", title="Fix RUF036 in target.py",
        category="capability",
        rationale="Fix a lint error", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="LINT_PRESENT", locator="maestro/target.py", expectation="RUF036",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(plan)

    stored = m.get_plan("lint_clean1")
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert "[ungrounded:no-such-lint]" in stored.obsolete_reason
    e._notifier.notify_plan_for_review.assert_not_awaited()
    assert len(gates.rows) == 1
    row = gates.rows[0]
    assert row.gate == "plan_grounding"
    assert row.action == "obsolete"
    assert "[ungrounded:no-such-lint]" in row.detail


@pytest.mark.asyncio
async def test_submit_for_review_grounded_lint_present_plan_falls_through(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a LINT_PRESENT plan whose ruff rule fires reaches the guards.

    Non-empty findings ⇒ grounded ⇒ the plan is saved ``pending_review``, the
    human is notified, and no gate-activation row is stamped.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    e._read_grounding_tree_text = (
        lambda probe: "maestro/target.py:1:1: RUF036 finding\n"
    )

    plan = EvolutionPlan(
        plan_id="lint_real1", title="Fix a real RUF036",
        category="capability",
        rationale="Fix a lint error", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="LINT_PRESENT", locator="maestro/target.py", expectation="RUF036",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(plan)

    stored = m.get_plan("lint_real1")
    assert stored is not None
    assert stored.status == "pending_review"
    e._notifier.notify_plan_for_review.assert_awaited_once()
    assert gates.rows == []


@pytest.mark.asyncio
async def test_submit_for_review_lint_present_none_read_fails_open(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a ``None`` grounding read fails open past the guard.

    When the reader returns ``None`` (the ruff read could not run, or the lint
    locator was unconfinable) the guard skips the refusal entirely: ``confirm`` is
    never called, and the plan is saved ``pending_review`` and the human notified —
    a lint probe that cannot be evaluated never false-refuses a plan. The no-op is
    still stamped as one ``unevaluated`` row (never ``obsolete``) so an empty
    refusal ledger cannot be confused with a probe that could never be read.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    e._read_grounding_tree_text = lambda probe: None

    plan = EvolutionPlan(
        plan_id="lint_none1", title="Fix RUF036 where ruff cannot run",
        category="capability",
        rationale="Fix a lint error", proposed_changes=["change"],
        files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="LINT_PRESENT", locator="maestro/target.py", expectation="RUF036",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    with patch("maestro.plan_grounding.confirm") as confirm_spy:
        await e.submit_for_review(plan)
        confirm_spy.assert_not_called()

    stored = m.get_plan("lint_none1")
    assert stored is not None
    assert stored.status == "pending_review"
    e._notifier.notify_plan_for_review.assert_awaited_once()
    assert len(gates.rows) == 1
    assert gates.rows[0].gate == "plan_grounding"
    assert gates.rows[0].action == "unevaluated"
    assert "[unevaluated] LINT_PRESENT" in gates.rows[0].detail


@pytest.mark.asyncio
async def test_submit_for_review_behavior_absent_unreadable_locator_is_unevaluated(
    mem_fs: dict[str, str],
) -> None:
    """A BEHAVIOR_ABSENT locator that cannot be read is a no-op, not a pass.

    The witness is invented by the same model that proposed the plan, so a wrong
    or non-existent path is the likely failure mode. The read seam returns
    ``None`` rather than ``""`` (which would have grounded the plan), so
    ``confirm`` never runs: the plan reaches the human unguarded — fail open — but
    the skip is stamped ``unevaluated``, keeping "no plan was ever refuted"
    distinguishable from "no probe was ever evaluated".
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    e._read_grounding_tree_text = lambda probe: None

    plan = EvolutionPlan(
        plan_id="behavior_none1", title="Build a settled-PR dequeue gate",
        category="capability",
        rationale="Nothing drops a settled PR from the queue",
        proposed_changes=["c"], files_affected=[], expected_impact="",
        grounding=GroundingProbe(
            kind="BEHAVIOR_ABSENT", locator="maestro/loop/no_such_file.py",
            expectation="self._is_pr_settled(",
        ),
        created_at="2025-01-01T00:00:00Z",
    )
    with patch("maestro.plan_grounding.confirm") as confirm_spy:
        await e.submit_for_review(plan)
        confirm_spy.assert_not_called()

    stored = m.get_plan("behavior_none1")
    assert stored is not None
    assert stored.status == "pending_review"
    e._notifier.notify_plan_for_review.assert_awaited_once()
    assert len(gates.rows) == 1
    assert gates.rows[0].action == "unevaluated"
    assert "[unevaluated] BEHAVIOR_ABSENT" in gates.rows[0].detail


def test_grounding_from_spec_lint_present_requires_rule_shape(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 114: a LINT_PRESENT probe needs a well-shaped ruff rule code.

    A valid rule (e.g. ``RUF036``) parses onto the probe (rule in ``expectation``,
    file path in ``locator``); a missing or malformed rule drops the probe to
    ``None`` so the plan proceeds unguarded — the safe direction — rather than
    issuing ``ruff --select <garbage>``.
    """
    e = _make_engine(mem_fs)
    e._config = replace(e._config, plan_grounding_enabled=True)

    probe = e._grounding_from_spec({
        "grounding": {
            "kind": "LINT_PRESENT", "locator": "maestro/target.py",
            "expectation": "RUF036",
        },
    })
    assert probe is not None
    assert probe.kind == "LINT_PRESENT"
    assert probe.locator == "maestro/target.py"
    assert probe.expectation == "RUF036"

    # A missing rule (empty expectation) → None.
    assert e._grounding_from_spec({
        "grounding": {"kind": "LINT_PRESENT", "locator": "maestro/target.py"},
    }) is None

    # A malformed rule (not a ruff code shape) → None.
    assert e._grounding_from_spec({
        "grounding": {
            "kind": "LINT_PRESENT", "locator": "maestro/target.py",
            "expectation": "not-a-rule",
        },
    }) is None


@pytest.mark.asyncio
async def test_submit_for_review_distinct_plan_persists_with_pending(
    mem_fs: dict[str, str],
) -> None:
    """A distinct plan still persists as pending even when another is open."""
    e, m, _c, j = _make(mem_fs)
    original = EvolutionPlan(
        plan_id="orig", title="Escalate unlanded remediation to Thomas",
        category="capability",
        rationale="Deliver one merge-or-close message when a lineage stalls past threshold",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(original)

    distinct = EvolutionPlan(
        plan_id="fresh", title="Add prometheus latency histogram to dashboard",
        category="capability",
        rationale="Instrument request duration buckets so operators can chart tail latency",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(distinct)

    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["fresh", "orig"]
    # Both proposals produced a normal "Proposing evolution" note — no refusal.
    notes = j.read_events_by_type("evolution_note")
    assert len(notes) == 2
    assert all("Refused to mint" not in n.content for n in notes)


@pytest.mark.asyncio
async def test_submit_for_review_refuses_recited_live_source_branch(
    mem_fs: dict[str, str],
) -> None:
    """A plan re-basing on a live plan's source_task_id lands obsolete.

    The content-word Jaccard guards key on body overlap, so a re-wrap of the
    same stranded task branch under distinct framing slips past them. This
    guard keys on the citation itself: the incoming plan's ``source_task_id``
    is already claimed by a live (``pending_review``/``approved``/
    ``implemented``) plan.
    """
    e, m, _c, j = _make(mem_fs)
    original = EvolutionPlan(
        plan_id="orig", title="Wire score_delivery_prediction sensor",
        category="capability",
        rationale="Add a behavioral sensor over remediation delivery accuracy",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        source_task_id="task-branch-1",
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(original)

    # Distinct title and rationale (would pass the Jaccard guards) but the
    # same underlying task branch.
    recite = EvolutionPlan(
        plan_id="recite", title="Emit a delivery-prediction accuracy gauge",
        category="capability",
        rationale="Surface how often a remediation's forecast matched its landing",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        source_task_id="task-branch-1",
        created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(recite)

    pending_ids = [p.plan_id for p in m.get_pending_plans()]
    assert pending_ids == ["orig"]
    dup = m.get_plan("recite")
    assert dup is not None
    assert dup.status == "obsolete"
    notes = j.read_events_by_type("evolution_note")
    assert len(notes) == 2
    assert "task-branch-1" in notes[1].content
    assert "re-bases on source branch" in notes[1].content


@pytest.mark.asyncio
async def test_submit_for_review_accepts_source_cited_only_by_rejected(
    mem_fs: dict[str, str],
) -> None:
    """A branch cited only by a rejected plan is re-citable; a null id passes."""
    e, m, _c, j = _make(mem_fs)
    # A rejected plan's citation does not consume the branch.
    m.save_evolution_plan(EvolutionPlan(
        plan_id="rej", title="Earlier attempt at the delivery sensor",
        category="capability",
        rationale="A prior framing Thomas rejected",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", source_task_id="task-branch-9",
        created_at="2025-01-01T00:00:00Z",
    ))

    recite_rejected = EvolutionPlan(
        plan_id="fresh-recite", title="Retry the delivery-prediction sensor",
        category="capability",
        rationale="Re-base on the branch a prior rejected plan named",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        source_task_id="task-branch-9",
        created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(recite_rejected)

    no_source = EvolutionPlan(
        plan_id="from-main", title="A plan based fresh from main",
        category="capability",
        rationale="No task branch nomination at all",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        source_task_id=None,
        created_at="2025-01-03T00:00:00Z",
    )
    await e.submit_for_review(no_source)

    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["fresh-recite", "from-main"]
    notes = j.read_events_by_type("evolution_note")
    assert all("Refused to mint" not in n.content for n in notes)


# Five themed pending plans whose dominant tokens
# (stand/awaiting/approval/remediation) all contain a small candidate's
# theme, yet whose unique vocabulary keeps each below the pairwise Jaccard
# cutoff — pre-seeded directly so they bypass the per-plan submit guards.
_THEMED_PENDING = [
    (
        "c1", "Stand on awaiting approval remediation lineages quietly",
        "Stop the merge-or-close churn and accept stillness this iteration",
    ),
    (
        "c2", "Stand on awaiting approval remediation without reworking again",
        "Halt repeated escalation drafting; the lineage already carries a verdict",
    ),
    (
        "c3", "Stand on awaiting approval remediation pending Thomas ruling",
        "Cease minting fresh sensors that only describe the same standoff",
    ),
    (
        "c4", "Stand on awaiting approval remediation backlog deliberately",
        "Decline another costume signature; choose empty hands over motion",
    ),
    (
        "c5", "Stand on awaiting approval remediation lineages once more",
        "Refuse the treadmill of rephrasing one unlanded standoff differently",
    ),
]


def _seed_themed_pending(m: Memory) -> None:
    for plan_id, title, rationale in _THEMED_PENDING:
        m.save_evolution_plan(EvolutionPlan(
            plan_id=plan_id, title=title,
            category="capability", rationale=rationale,
            proposed_changes=["change"], files_affected=[],
            expected_impact="", created_at="2025-01-01T00:00:00Z",
        ))


@pytest.mark.asyncio
async def test_submit_for_review_blocks_themed_cluster(
    mem_fs: dict[str, str],
) -> None:
    """A sixth themed restatement lands obsolete via the cluster guard.

    The five pre-seeded plans each escape the pairwise duplicate guard, but
    together they form a theme cluster. A small candidate fully contained by
    that theme is recorded obsolete (not a sixth pending burden) with a
    journal note citing the cluster.
    """
    e, m, _c, j = _make(mem_fs)
    _seed_themed_pending(m)

    candidate = EvolutionPlan(
        plan_id="sixth", title="Stand awaiting approval remediation",
        category="capability",
        rationale="Stand awaiting approval remediation",
        proposed_changes=["change"], files_affected=[],
        expected_impact="", created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(candidate)

    # The five originals stay pending; the candidate is obsolete, not a
    # sixth pending_review row.
    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["c1", "c2", "c3", "c4", "c5"]
    sixth = m.get_plan("sixth")
    assert sixth is not None
    assert sixth.status == "obsolete"
    notes = j.read_events_by_type("evolution_note")
    assert len(notes) == 1
    assert "themed" in notes[0].content
    assert "c1" in notes[0].content


@pytest.mark.asyncio
async def test_submit_for_review_non_themed_plan_mints_despite_cluster(
    mem_fs: dict[str, str],
) -> None:
    """A distinct plan still mints normally even amid a themed cluster."""
    e, m, _c, j = _make(mem_fs)
    # Pin the saturation submit-block above the seeded backlog so this test
    # isolates the theme-cluster guard: a distinct plan must still mint even
    # when five themed plans already sit pending, independent of the committed
    # production cap.
    e._config = Config(loop_pending_plan_submit_block_threshold=20)
    _seed_themed_pending(m)

    distinct = EvolutionPlan(
        plan_id="fresh", title="Add prometheus latency histogram to dashboard",
        category="capability",
        rationale="Instrument request duration buckets so operators chart tail latency",
        proposed_changes=["change"], files_affected=[],
        expected_impact="", created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(distinct)

    fresh = m.get_plan("fresh")
    assert fresh is not None
    assert fresh.status == "pending_review"
    notes = j.read_events_by_type("evolution_note")
    assert len(notes) == 1
    assert "Proposing evolution" in notes[0].content


async def _seed_cluster_pair(e: EvolutionEngine) -> None:
    """Submit two pending plans that each theme-cluster with the candidate.

    Token sets (title + rationale, content-word Jaccard machinery):
    ``c1`` = {alpha, bravo, charlie, foxtrot, golf}, ``c2`` =
    {alpha, bravo, delta, hotel, india}. Against the candidate
    {alpha, bravo, charlie, delta, echo} each scores Jaccard 3/7 ≈ 0.43 —
    above the looser cluster threshold (0.35) but below the strict
    duplicate threshold (0.5), and only 0.25 against *each other* so both
    persist as ``pending_review`` rather than blocking one another.
    """
    p1 = EvolutionPlan(
        plan_id="c1", title="Charlie Golf",
        category="capability", rationale="alpha bravo foxtrot",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    p2 = EvolutionPlan(
        plan_id="c2", title="Delta Hotel",
        category="capability", rationale="alpha bravo india",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(p1)
    await e.submit_for_review(p2)


def _cluster_candidate() -> EvolutionPlan:
    """The candidate plan that clusters with both seeded plans."""
    return EvolutionPlan(
        plan_id="cand", title="Alpha Bravo",
        category="capability", rationale="charlie delta echo",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-03T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_submit_for_review_blocks_pending_cluster_at_cap(
    mem_fs: dict[str, str],
) -> None:
    """A candidate clustering with >= cap live plans lands obsolete.

    Each seeded plan stays *below* the strict duplicate threshold (0.5) yet
    meets the looser cluster threshold (0.35), so the single-plan duplicate
    guard never fires — only the cluster-level stop does. The default cluster
    settings (cluster_max=2, threshold=0.35) drive the gate.
    """
    e, m, _c, j = _make(mem_fs)
    await _seed_cluster_pair(e)
    await e.submit_for_review(_cluster_candidate())

    # No third pending row: the two seeds stay pending, the candidate is obsolete.
    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["c1", "c2"]
    cand = m.get_plan("cand")
    assert cand is not None
    assert cand.status == "obsolete"
    # The refusal note names the cluster size and is tagged for the cluster stop.
    notes = j.read_events_by_type("evolution_note")
    refusals = [n for n in notes if "theme-clusters with 2 live plans" in n.content]
    assert len(refusals) == 1


@pytest.mark.asyncio
async def test_submit_for_review_below_cluster_cap_mints_pending(
    mem_fs: dict[str, str],
) -> None:
    """A candidate clustering with fewer than cap pending plans still mints."""
    e, m, _c, _j = _make(mem_fs)
    only = EvolutionPlan(
        plan_id="c1", title="Charlie Golf",
        category="capability", rationale="alpha bravo foxtrot",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(only)
    await e.submit_for_review(_cluster_candidate())

    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["c1", "cand"]
    cand = m.get_plan("cand")
    assert cand is not None
    assert cand.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_for_review_cluster_stop_disabled_when_max_non_positive(
    mem_fs: dict[str, str],
) -> None:
    """``pending_cluster_max <= 0`` disables the cluster stop entirely."""
    e, m, _c, _j = _make(mem_fs)
    e._config = Config(loop_pending_cluster_max=0)
    await _seed_cluster_pair(e)
    await e.submit_for_review(_cluster_candidate())

    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["c1", "c2", "cand"]
    cand = m.get_plan("cand")
    assert cand is not None
    assert cand.status == "pending_review"


def _distinct_pending(i: int) -> EvolutionPlan:
    """A pending_review plan with vocabulary unique to ``i``.

    Distinct title and content words per index so seeded plans never trip the
    title-exact dedup, the duplicate-pending guard, or the theme-cluster stop —
    isolating the backlog-saturation submit-block under test.
    """
    return EvolutionPlan(
        plan_id=f"sat{i}", title=f"Saturation seed number {i}",
        category="capability",
        rationale=f"topic{i} subject{i} matter{i} area{i} field{i} scope{i}",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="pending_review", created_at=f"2025-01-0{i}T00:00:00Z",
    )


def _saturation_candidate() -> EvolutionPlan:
    """A wholly distinct candidate that only the total-count cap can block."""
    return EvolutionPlan(
        plan_id="newcand", title="Wholly novel forward proposal",
        category="capability",
        rationale="unique novel fresh original independent unrelated wording",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-02-01T00:00:00Z",
    )


def _implemented_open_pr_plan() -> EvolutionPlan:
    """An implemented plan behind an open PR — live, but not pending_review.

    Its vocabulary is distinct from every saturation seed and the candidate, so
    it counts toward the *live* corpus without tripping any content guard.
    """
    return EvolutionPlan(
        plan_id="impl-open", title="Implemented plan behind an open PR",
        category="capability",
        rationale="delta epsilon zeta eta theta iota kappa lambda",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="implemented", created_at="2025-01-20T00:00:00Z",
        pr_url="https://github.com/o/r/pull/321",
    )


def _approved_twin() -> EvolutionPlan:
    """An approved plan — live, but out of the pending_review corpus."""
    return EvolutionPlan(
        plan_id="approved-twin",
        title="Escalate unlanded remediation to Thomas",
        category="capability",
        rationale="Deliver one merge-or-close message when a lineage stalls past threshold",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="approved", created_at="2025-01-15T00:00:00Z",
    )


def _restatement_candidate() -> EvolutionPlan:
    """A near-restatement of :func:`_approved_twin` (different title, same meaning)."""
    return EvolutionPlan(
        plan_id="restate",
        title="Escalate unlanded remediation to Thomas once",
        category="capability",
        rationale=(
            "Deliver a single merge-or-close message when the lineage stalls past threshold"
        ),
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-02-02T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_submit_for_review_refuses_at_saturation_threshold(
    mem_fs: dict[str, str],
) -> None:
    """At/above the submit-block threshold a distinct candidate lands obsolete.

    Seeds three distinct pending plans, sets the cap to 3, then submits a
    candidate that trips no content-based guard — only the total-count cap can
    refuse it. The candidate is recorded ``obsolete`` (its existing non-accepting
    return shape), the pending backlog is unchanged, and the refusal reason
    names the saturation.
    """
    e, m, _c, j = _make(mem_fs)
    e._config = Config(
        loop_pending_plan_submit_block_threshold=3, generation_admission_enabled=False
    )
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(_saturation_candidate())

    # No fourth pending row: the three seeds stay pending, candidate obsolete.
    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["sat1", "sat2", "sat3"]
    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    # The refusal reason and journal note carry the saturation explanation.
    assert "saturated" in (cand.obsolete_reason or "")
    assert ">= 3 cap" in (cand.obsolete_reason or "")
    notes = j.read_events_by_type("evolution_note")
    refusals = [n for n in notes if "backlog is saturated at 3 plans" in n.content]
    assert len(refusals) == 1


@pytest.mark.asyncio
async def test_submit_for_review_accepts_below_saturation_threshold(
    mem_fs: dict[str, str],
) -> None:
    """Below the threshold an otherwise-valid candidate mints a pending row."""
    e, m, _c, _j = _make(mem_fs)
    e._config = Config(loop_pending_plan_submit_block_threshold=3)
    for i in (1, 2):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(_saturation_candidate())

    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["newcand", "sat1", "sat2"]
    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_for_review_saturation_disabled_when_threshold_non_positive(
    mem_fs: dict[str, str],
) -> None:
    """``pending_plan_submit_block_threshold <= 0`` disables the production cap."""
    e, m, _c, _j = _make(mem_fs)
    e._config = Config(loop_pending_plan_submit_block_threshold=0)
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(_saturation_candidate())

    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["newcand", "sat1", "sat2", "sat3"]
    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "pending_review"


def test_generation_admission_directive_relocated_byte_identical() -> None:
    """120: the submission-door directive is now the single relocated constant in
    ``generation_gate`` — byte-identical, so the door and the pre-mint gate draw
    the same value from one owner."""
    from maestro.evolution_submission import _GENERATION_ADMISSION_DIRECTIVE
    from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE

    assert _GENERATION_ADMISSION_DIRECTIVE == GENERATION_STANDDOWN_DIRECTIVE


@pytest.mark.asyncio
async def test_admission_gate_refuses_saturated_backlog_writes_directive(
    mem_fs: dict[str, str],
) -> None:
    """Flag on + saturated backlog: the candidate lands ``obsolete`` and a
    prune/land directive is written into the empty ``next_task_directive`` slot.
    """
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=3,
    )
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(_saturation_candidate())

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    assert "Admission gate" in (cand.obsolete_reason or "")
    directive = c.load_self_model().next_task_directive or ""
    assert "mark_plan_obsolete" in directive
    assert len(directive) <= Consciousness.MAX_DIRECTIVE_CHARS
    # The three seeds stay pending; no fourth pending row is minted.
    pending_ids = sorted(p.plan_id for p in m.get_pending_plans())
    assert pending_ids == ["sat1", "sat2", "sat3"]


@pytest.mark.asyncio
async def test_admission_gate_preserves_prior_directive_first_writer_wins(
    mem_fs: dict[str, str],
) -> None:
    """First-writer-wins negative branch: when a prior gate/bridge already owns
    ``next_task_directive``, the admission gate still records the candidate
    ``obsolete`` but leaves the existing directive untouched — it does *not*
    overwrite it with ``_GENERATION_ADMISSION_DIRECTIVE``.

    The other admission tests all start from an empty slot, so they line-cover the
    write path but never assert the preservation branch. An inverted guard
    (``if model.next_task_directive:`` overwriting an existing directive) would
    pass line-based coverage undetected; this pins the guard.
    """
    from maestro.evolution_submission import _GENERATION_ADMISSION_DIRECTIVE

    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=3,
    )
    # A prior gate/bridge already seeded the slot with a distinct sentinel
    # (short, so ``save_self_model``'s MAX_DIRECTIVE_CHARS truncation is a no-op).
    prior = "Prior gate owns this: finish PR #123 before minting more."
    model = c.load_self_model()
    model.next_task_directive = prior
    c.save_self_model(model)

    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(_saturation_candidate())

    # The refusal is unconditional: the candidate is still recorded obsolete...
    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    assert "Admission gate" in (cand.obsolete_reason or "")
    # ...but the pre-existing directive is preserved, not overwritten.
    directive = c.load_self_model().next_task_directive
    assert directive == prior
    assert directive != _GENERATION_ADMISSION_DIRECTIVE


@pytest.mark.asyncio
async def test_admission_gate_themed_graveyard_alone_no_longer_refuses(
    mem_fs: dict[str, str],
) -> None:
    """Flag on + a themed obsolete graveyard: with the theme arm retired
    (proposal 155 Step 3) a themed candidate below the backlog cap is admitted."""
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=100,  # backlog not saturated
        generation_admission_theme_fraction=0.75,
        generation_admission_window=20,
    )
    for i in (1, 2, 3, 4, 5):
        m.save_evolution_plan(_themed_obsolete(i))
    await e.submit_for_review(_themed_candidate())

    cand = m.get_plan("themedcand")
    assert cand is not None
    assert cand.status == "pending_review"
    assert c.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_admission_gate_admits_when_neither_condition_holds(
    mem_fs: dict[str, str],
) -> None:
    """Flag on but neither arm fires (non-themed candidate, unsaturated backlog):
    a pending row is minted and no directive is written."""
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=100,
    )
    m.save_evolution_plan(_distinct_pending(1))
    await e.submit_for_review(_saturation_candidate())

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "pending_review"
    assert c.load_self_model().next_task_directive is None


def _future_experimental_flag() -> str:
    """An EXPERIMENTAL_FLAGS key whose review_by is still future vs. FREEZE_DATE.

    Selected against the frozen date, not the wall clock, so the tests stay
    green on a frozen repo after the last manifest horizon passes; the
    production guard's clock is pinned to FREEZE_DATE by ``_frozen_clock``.
    """
    from maestro.flag_lifecycle import EXPERIMENTAL_FLAGS, FREEZE_DATE, parse_review_by

    return next(
        f.flag for f in EXPERIMENTAL_FLAGS
        if parse_review_by(f.review_by) >= FREEZE_DATE
    )


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` reads ``flag_lifecycle.FREEZE_DATE``."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _FrozenDatetime:
        from maestro.flag_lifecycle import FREEZE_DATE

        return cls(FREEZE_DATE.year, FREEZE_DATE.month, FREEZE_DATE.day, tzinfo=tz or UTC)


def _frozen_clock() -> AbstractContextManager[object]:
    """Pin ``maestro.evolution_submission``'s wall clock to FREEZE_DATE."""
    return patch("maestro.evolution_submission.datetime", _FrozenDatetime)


def _graduation_plan(plan_id: str = "grad1") -> EvolutionPlan:
    """A plan proposing to flip a future-window experimental flag on."""
    flag = _future_experimental_flag()
    return EvolutionPlan(
        plan_id=plan_id, title=f"Graduate {flag} to default-on",
        category="capability",
        rationale="flip it", proposed_changes=[f"flip {flag}"],
        files_affected=["maestro/config.py"], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_submit_for_review_refuses_premature_flag_graduation(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 2: a self_initiated flag-graduation plan lands obsolete.

    The plan is persisted ``obsolete`` with an ``[flag-graduation-premature:...]``
    reason, is absent from ``get_pending_plans``, a ``flag_graduation_grounding``
    gate-activation row is stamped, and the human is never notified.
    """
    e, m, _c, _j = _make(mem_fs)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates
    plan = _graduation_plan()

    with _frozen_clock():
        await e.submit_for_review(plan, self_initiated=True)

    stored = m.get_plan("grad1")
    assert stored is not None
    assert stored.status == "obsolete"
    assert stored.obsolete_reason is not None
    assert "[flag-graduation-premature:" in stored.obsolete_reason
    assert m.get_pending_plans() == []
    e._notifier.notify_plan_for_review.assert_not_awaited()
    assert len(gates.rows) == 1
    row = gates.rows[0]
    assert row.gate == "flag_graduation_grounding"
    assert row.action == "obsolete"
    assert "[flag-graduation-premature:" in row.detail


@pytest.mark.asyncio
async def test_submit_for_review_non_self_initiated_bypasses_flag_graduation(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 6: a non-self_initiated flag-graduation plan is admitted."""
    e, m, _c, _j = _make(mem_fs)
    e._notifier.notify_plan_for_review = AsyncMock()
    gates = _RecordingGateBackend()
    e._gate_activations = gates

    with _frozen_clock():
        await e.submit_for_review(_graduation_plan("grad2"), self_initiated=False)

    stored = m.get_plan("grad2")
    assert stored is not None
    assert stored.status == "pending_review"
    assert [p.plan_id for p in m.get_pending_plans()] == ["grad2"]
    graduation_rows = [
        r for r in gates.rows if r.gate == "flag_graduation_grounding"
    ]
    assert graduation_rows == []


@pytest.mark.asyncio
async def test_admission_gate_flag_off_saturation_uses_legacy_block_no_directive(
    mem_fs: dict[str, str],
) -> None:
    """Flag off: a saturated backlog is refused by the pre-existing submit-block
    (obsolete, wording unchanged) and no admission directive is written —
    byte-identical to today."""
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        generation_admission_enabled=False,
        loop_pending_plan_submit_block_threshold=3,
    )
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(_saturation_candidate())

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    # The legacy submit-block wording, not the admission gate's.
    assert "saturated" in (cand.obsolete_reason or "")
    assert "Admission gate" not in (cand.obsolete_reason or "")
    assert c.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_admission_gate_non_themed_candidate_admitted_over_themed_graveyard(
    mem_fs: dict[str, str],
) -> None:
    """Flag on + a theme-dominated obsolete graveyard and a non-themed
    candidate: admitted (minted pending, no directive)."""
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=100,  # backlog not saturated
        generation_admission_theme_fraction=0.75,
        generation_admission_window=20,
    )
    for i in (1, 2, 3, 4, 5):
        m.save_evolution_plan(_themed_obsolete(i))
    await e.submit_for_review(_saturation_candidate())  # non-themed candidate

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "pending_review"
    assert c.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_admission_gate_live_corpus_saturation_names_only_saturation(
    mem_fs: dict[str, str],
) -> None:
    """Admission gate on the live-work corpus: a themed candidate over a
    saturated live backlog and a theme-dominated graveyard is refused on
    saturation alone — the theme arm is retired (proposal 155 Step 3), and the
    saturation wording stays byte-identical.
    """
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=3,
        generation_admission_theme_fraction=0.75,
        generation_admission_window=20,
    )
    # Three distinct (non-themed) live pending plans saturate the live backlog…
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    # …while the obsolete graveyard is wholly self-measurement-themed.
    for i in (4, 5, 6):
        m.save_evolution_plan(_themed_obsolete(i))
    await e.submit_for_review(_themed_candidate())

    cand = m.get_plan("themedcand")
    assert cand is not None
    assert cand.status == "obsolete"
    reason = cand.obsolete_reason or ""
    assert reason == (
        "Admission gate refused net-new mint: backlog saturated (3 >= 3) — "
        "land or dispose an existing plan before proposing more"
    )
    directive = c.load_self_model().next_task_directive or ""
    assert "mark_plan_obsolete" in directive
    # The three live seeds stay pending; the candidate never reached the submit.
    assert sorted(p.plan_id for p in m.get_pending_plans()) == [
        "sat1", "sat2", "sat3",
    ]


# ---------------------------------------------------------------------------
# Proposal 120, step 6 — demote the admission gate to a backstop for the
# pre-gated self-initiated channels; the deeper content-blind submit-block and
# the non-pre-gated (external) path keep the gate as a defense-in-depth backstop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_gate_demoted_self_initiated_falls_to_submit_block(
    mem_fs: dict[str, str],
) -> None:
    """Flag on + a pre-gated ``self_initiated`` submission bypasses the admission
    gate on a saturated backlog, but the deeper content-blind submit-block still
    catches it: obsolete with the *legacy* saturation wording (not the admission
    gate's), and no directive written.

    Proves the admission gate specifically is bypassed for a pre-gated
    self-initiated plan while the deepest backstop still holds. A saturated
    backlog cannot yield ``pending_review`` here — the admission gate and the
    submit-block read the same threshold and live-plan count, so bypassing the
    admission gate hands the candidate to the still-active submit-block.
    """
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        pre_mint_generation_gate_enabled=True,
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=3,
    )
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(
        _saturation_candidate(), self_initiated=True,
    )

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    # Refused by the legacy submit-block, not the demoted admission gate.
    assert "saturated" in (cand.obsolete_reason or "")
    assert "Admission gate" not in (cand.obsolete_reason or "")
    # The submit-block writes no directive.
    assert c.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_admission_gate_self_initiated_themed_candidate_with_claim_admitted(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 155 Step 3: a pre-gated ``self_initiated`` themed candidate with
    an admitted value claim over a theme-dominated graveyard is admitted — the
    value door, not the retired theme arm, judges the candidate."""
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        pre_mint_generation_gate_enabled=True,
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=100,
        generation_admission_theme_fraction=0.75,
        generation_admission_window=20,
    )
    for i in (1, 2, 3, 4, 5):
        m.save_evolution_plan(_themed_obsolete(i))
    await e.submit_for_review(_themed_candidate(), self_initiated=True)

    cand = m.get_plan("themedcand")
    assert cand is not None
    assert cand.status == "pending_review"
    assert c.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_admission_gate_self_initiated_theme_arm_admits_non_themed_candidate(
    mem_fs: dict[str, str],
) -> None:
    """The same theme-dominated graveyard admits a non-themed self-initiated
    candidate carrying an admitted claim."""
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        pre_mint_generation_gate_enabled=True,
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=100,
        generation_admission_theme_fraction=0.75,
        generation_admission_window=20,
    )
    for i in (1, 2, 3, 4, 5):
        m.save_evolution_plan(_themed_obsolete(i))
    await e.submit_for_review(
        _saturation_candidate(), self_initiated=True,
    )

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "pending_review"
    assert c.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_admission_gate_backstop_survives_external_path(
    mem_fs: dict[str, str],
) -> None:
    """Flag on but ``self_initiated=False`` (the default / external path): the
    admission gate still fires as the retained backstop for a non-pre-gated
    submission — obsolete with the admission wording and the prune/land directive
    written."""
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        pre_mint_generation_gate_enabled=True,
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=3,
    )
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(_saturation_candidate())

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    assert "Admission gate" in (cand.obsolete_reason or "")
    directive = c.load_self_model().next_task_directive or ""
    assert "mark_plan_obsolete" in directive


@pytest.mark.asyncio
async def test_admission_gate_demotion_inert_without_pre_mint_flag(
    mem_fs: dict[str, str],
) -> None:
    """Flag *off*: with ``pre_mint_generation_gate_enabled=False`` the added
    conjunct is inert, so even a ``self_initiated=True`` submission still hits the
    admission gate — proving the demotion is a no-op without the pre-mint flag.
    """
    e, m, c, _j = _make(mem_fs)
    e._config = Config(
        pre_mint_generation_gate_enabled=False,
        generation_admission_enabled=True,
        loop_pending_plan_submit_block_threshold=3,
    )
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))
    await e.submit_for_review(
        _saturation_candidate(), self_initiated=True,
    )

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    assert "Admission gate" in (cand.obsolete_reason or "")
    directive = c.load_self_model().next_task_directive or ""
    assert "mark_plan_obsolete" in directive


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_counts_implemented_at_saturation(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 100 regression anchor: an implemented open-PR plan counts as live.

    Nineteen pending plans plus one implemented open-PR plan at a submit-block
    threshold of 20: the pending-review rows (19) alone are below the cap, but the
    live-work count (20) is at it, so the candidate is refused (recorded
    ``obsolete``) — the implemented plan tips the count to the cap.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = Config(loop_pending_plan_submit_block_threshold=20)
    for i in range(1, 20):
        m.save_evolution_plan(_distinct_pending(i))
    m.save_evolution_plan(_implemented_open_pr_plan())

    await e.submit_for_review(_saturation_candidate())

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    assert "saturated" in (cand.obsolete_reason or "")


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_catches_approved_restatement(
    mem_fs: dict[str, str],
) -> None:
    """A near-restatement of an approved plan is refused at minting (proposal 100)."""
    e, m, _c, _j = _make(mem_fs)
    e._config = Config()
    m.save_evolution_plan(_approved_twin())

    await e.submit_for_review(_restatement_candidate())

    cand = m.get_plan("restate")
    assert cand is not None
    assert cand.status == "obsolete"
    # No fresh pending row: the approved twin blocked it via the live corpus.
    assert [p.plan_id for p in m.get_pending_plans()] == []


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_saturation_wording_is_honest(
    mem_fs: dict[str, str],
) -> None:
    """The saturation record names the live-work corpus, not pending.

    Proposal 100 (overflow nit of-f9ac893b): ``pending_count`` tallies
    approved/implemented plans too, so the recorded obsolete-reason and journal
    note must describe the live-work backlog rather than falsely asserting a pure
    ``pending_review`` backlog — the "Journal over logs — keep the journal honest"
    invariant.
    """
    e, m, _c, j = _make(mem_fs)
    e._config = Config(
        loop_pending_plan_submit_block_threshold=20, generation_admission_enabled=False
    )
    for i in range(1, 20):
        m.save_evolution_plan(_distinct_pending(i))
    m.save_evolution_plan(_implemented_open_pr_plan())

    await e.submit_for_review(_saturation_candidate())

    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "obsolete"
    reason = cand.obsolete_reason or ""
    assert "Live-work backlog saturated" in reason
    assert "Pending-review" not in reason
    notes = j.read_events_by_type("evolution_note")
    honest = [n for n in notes if "live-work backlog is saturated" in n.content]
    assert len(honest) == 1
    false_notes = [
        n for n in notes if "pending_review backlog is saturated" in n.content
    ]
    assert false_notes == []


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_duplicate_wording_is_honest(
    mem_fs: dict[str, str],
) -> None:
    """Refusing a restatement of an approved plan names it 'live'.

    Proposal 100 (overflow nit of-f9ac893b): the approved twin has been ruled on
    by Thomas and may sit behind an open PR, so neither the obsolete-reason nor
    the journal note may claim it is a ``pending`` plan "awaiting Thomas's
    review" / one Thomas "has not yet ruled on".
    """
    e, m, _c, j = _make(mem_fs)
    e._config = Config()
    m.save_evolution_plan(_approved_twin())

    await e.submit_for_review(_restatement_candidate())

    cand = m.get_plan("restate")
    assert cand is not None
    assert cand.status == "obsolete"
    reason = cand.obsolete_reason or ""
    assert "live plan" in reason
    assert "already in flight" in reason
    assert "awaiting Thomas's review" not in reason
    notes = j.read_events_by_type("evolution_note")
    refusals = [
        n for n in notes
        if "Refused to mint duplicate evolution plan" in n.content
    ]
    assert len(refusals) == 1
    assert "near-restates live plan" in refusals[0].content
    assert "still in flight" in refusals[0].content
    assert "not yet ruled on" not in refusals[0].content


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_theme_cluster_wording_is_honest(
    mem_fs: dict[str, str],
) -> None:
    """The themed-cluster refusal names 'live plans', not pending.

    Proposal 100 (overflow nit of-f9ac893b): the theme-cluster guard's record
    must describe the live-work corpus honestly rather than asserting the matched
    plans are ``pending`` and "awaiting Thomas's review".
    """
    e, m, _c, j = _make(mem_fs)
    e._config = Config()
    _seed_themed_pending(m)

    candidate = EvolutionPlan(
        plan_id="sixth", title="Stand awaiting approval remediation",
        category="capability",
        rationale="Stand awaiting approval remediation",
        proposed_changes=["change"], files_affected=[],
        expected_impact="", created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(candidate)

    sixth = m.get_plan("sixth")
    assert sixth is not None
    assert sixth.status == "obsolete"
    reason = sixth.obsolete_reason or ""
    assert "live plans" in reason
    assert "already in flight" in reason
    assert "awaiting Thomas's review" not in reason
    notes = j.read_events_by_type("evolution_note")
    themed = [n for n in notes if "themed evolution plan" in n.content]
    assert len(themed) == 1
    assert "live plans" in themed[0].content
    assert "not yet ruled on" not in themed[0].content


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_pending_cluster_wording_is_honest(
    mem_fs: dict[str, str],
) -> None:
    """The pending-cluster-cap refusal names 'live plans', not pending.

    Proposal 100 (overflow nit of-f9ac893b): the ``find_pending_plan_cluster``
    stop's obsolete-reason and journal note must name the live-work corpus, not
    falsely assert the matched plans await Thomas's review.
    """
    e, m, _c, j = _make(mem_fs)
    e._config = Config()
    await _seed_cluster_pair(e)

    await e.submit_for_review(_cluster_candidate())

    cand = m.get_plan("cand")
    assert cand is not None
    assert cand.status == "obsolete"
    reason = cand.obsolete_reason or ""
    assert "live plans" in reason
    assert "already in flight" in reason
    assert "awaiting Thomas's review" not in reason
    notes = j.read_events_by_type("evolution_note")
    refusals = [
        n for n in notes if "theme-clusters with 2 live plans" in n.content
    ]
    assert len(refusals) == 1
    assert "awaiting Thomas's review" not in refusals[0].content


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_theme_cluster_catches_approved(
    mem_fs: dict[str, str],
) -> None:
    """The theme-cluster guard consults the live corpus, so an approved cluster fires.

    Proposal 100: the themed cluster here is seeded ``approved`` (a live status
    outside ``pending_review``), so the guard can see it — and fire — only because
    ``find_theme_cluster_pending_plans`` reads the whole live-work corpus. A
    candidate restating the approved cluster is therefore refused (recorded
    ``obsolete``); a pending-only corpus would leave it un-refused.
    """
    e, m, _c, j = _make(mem_fs)
    e._config = Config()
    # Same themed cluster as ``_seed_themed_pending``, but recorded ``approved`` —
    # a live status outside the ``pending_review`` corpus.
    for plan_id, title, rationale in _THEMED_PENDING:
        m.save_evolution_plan(EvolutionPlan(
            plan_id=plan_id, title=title,
            category="capability", rationale=rationale,
            proposed_changes=["change"], files_affected=[],
            expected_impact="", status="approved",
            created_at="2025-01-01T00:00:00Z",
        ))

    candidate = EvolutionPlan(
        plan_id="sixth", title="Stand awaiting approval remediation",
        category="capability",
        rationale="Stand awaiting approval remediation",
        proposed_changes=["change"], files_affected=[],
        expected_impact="", created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(candidate)

    sixth = m.get_plan("sixth")
    assert sixth is not None
    assert sixth.status == "obsolete"
    # The theme-cluster guard (not the pending-cluster-cap guard) is the one that
    # fired: its record uses the "Themed restatement of" reason wording.
    assert "Themed restatement of" in (sixth.obsolete_reason or "")
    # No fresh pending row: the approved cluster blocked it via the live corpus.
    assert [p.plan_id for p in m.get_pending_plans()] == []
    notes = j.read_events_by_type("evolution_note")
    themed = [n for n in notes if "themed evolution plan" in n.content]
    assert len(themed) == 1
    assert "live plans" in themed[0].content


@pytest.mark.asyncio
async def test_submit_for_review_live_corpus_pending_cluster_catches_live(
    mem_fs: dict[str, str],
) -> None:
    """The pending-cluster-cap guard consults the live corpus.

    Proposal 100: the cluster pair here is seeded ``approved`` + ``implemented``
    (an open-PR plan) — both outside ``pending_review`` — so the cap can reach two
    members, and fire, only because ``find_pending_plan_cluster`` reads the
    live-work corpus. Each seed sits at containment 0.6 (not > the 0.6
    theme-cluster fraction), so the earlier theme-cluster guard passes and the
    pending-cluster-cap guard is the one under test; a pending-only corpus would
    leave the candidate un-refused.
    """
    e, m, _c, j = _make(mem_fs)
    e._config = Config()
    # The ``_seed_cluster_pair`` token construction, recorded as non-pending live
    # plans (one ``approved``, one ``implemented`` behind an open PR).
    m.save_evolution_plan(EvolutionPlan(
        plan_id="c1", title="Charlie Golf",
        category="capability", rationale="alpha bravo foxtrot",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="approved", created_at="2025-01-01T00:00:00Z",
    ))
    m.save_evolution_plan(EvolutionPlan(
        plan_id="c2", title="Delta Hotel",
        category="capability", rationale="alpha bravo india",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="implemented", created_at="2025-01-02T00:00:00Z",
        pr_url="https://github.com/o/r/pull/999",
    ))

    await e.submit_for_review(_cluster_candidate())

    cand = m.get_plan("cand")
    assert cand is not None
    assert cand.status == "obsolete"
    # The pending-cluster-cap guard fired against the live (approved/implemented)
    # pair, naming the live corpus honestly.
    assert "Theme-clusters with 2 live plans" in (cand.obsolete_reason or "")
    # Routed through the single door-refusal stamper (#4157).
    assert cand.refused_at_submission is True
    # No fresh pending row: the live pair blocked it via the live corpus.
    assert [p.plan_id for p in m.get_pending_plans()] == []
    notes = j.read_events_by_type("evolution_note")
    refusals = [n for n in notes if "theme-clusters with 2 live plans" in n.content]
    assert len(refusals) == 1


@pytest.mark.asyncio
async def test_submit_for_review_saturation_leaves_disposal_lever_open(
    mem_fs: dict[str, str],
) -> None:
    """mark_plan_obsolete still disposes plans while the backlog is saturated.

    The production cap binds ``submit_for_review`` only; the owned disposal
    lever is never blocked, so landing/disposing an existing plan is the
    forward path the cap intends.
    """
    e, m, _c, _j = _make(mem_fs)
    e._config = Config(loop_pending_plan_submit_block_threshold=3)
    for i in (1, 2, 3):
        m.save_evolution_plan(_distinct_pending(i))

    # Disposal works under saturation, dropping the backlog below the cap...
    m.mark_plan_obsolete("sat1", "disposed by owner while saturated")
    assert m.get_plan("sat1").status == "obsolete"
    assert sorted(p.plan_id for p in m.get_pending_plans()) == ["sat2", "sat3"]

    # ...which reopens the forward path: a new plan now mints.
    await e.submit_for_review(_saturation_candidate())
    cand = m.get_plan("newcand")
    assert cand is not None
    assert cand.status == "pending_review"


@pytest.mark.asyncio
async def test_submit_for_review_cluster_stop_duplicate_id_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A same-id resubmission that trips the cluster stop is absorbed idempotently.

    A pre-persisted ``obsolete`` row under the candidate's id escapes the
    active title-exact dedup (obsolete is excluded), so the candidate reaches
    the cluster branch; the ``save_evolution_plan`` there raises
    ``DuplicatePlanError`` on the colliding id and the idempotent retry path
    swallows it without raising or writing a second row.
    """
    e, m, _c, _j = _make(mem_fs)
    await _seed_cluster_pair(e)
    m.save_evolution_plan(EvolutionPlan(
        plan_id="cand", title="Zulu Yankee",
        category="capability", rationale="entirely unrelated body text",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="obsolete", created_at="2025-01-02T12:00:00Z",
    ))
    await e.submit_for_review(_cluster_candidate())  # must not raise

    matching = [p for p in m.get_all_plans() if p.plan_id == "cand"]
    assert len(matching) == 1
    assert matching[0].title == "Zulu Yankee"
    assert matching[0].status == "obsolete"


@pytest.mark.asyncio
async def test_submit_for_review_duplicate_plan_id_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A same-plan_id resubmission with a distinct body is idempotently absorbed.

    Distinct title + rationale lets it escape both the title-exact dedup and
    the restatement gate, so it reaches ``save_evolution_plan``'s duplicate-id
    raise and is swallowed as a ``DuplicatePlanError`` — saved once, no raise.
    """
    e, m, _c, _j = _make(mem_fs)
    first = EvolutionPlan(
        plan_id="dup-id", title="Alpha subsystem refactor",
        category="capability",
        rationale="Restructure the alpha module into cohesive units",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    await e.submit_for_review(first)
    resubmit = EvolutionPlan(
        plan_id="dup-id", title="Bravo telemetry exporter",
        category="capability",
        rationale="Emit prometheus histograms for outbound request latency",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(resubmit)  # must not raise

    matching = [p for p in m.get_all_plans() if p.plan_id == "dup-id"]
    assert len(matching) == 1
    assert matching[0].title == "Alpha subsystem refactor"


@pytest.mark.asyncio
async def test_submit_for_review_duplicate_pending_idempotent_retry(
    mem_fs: dict[str, str],
) -> None:
    """Re-submitting an already-recorded duplicate writes no second note.

    The first restatement is recorded ``obsolete`` with a refusal note. A
    second submission of the same ``plan_id`` re-enters the duplicate-pending
    guard, but ``save_evolution_plan`` raises ``DuplicatePlanError`` (the row
    exists), so ``_record_obsolete_duplicate`` returns ``False`` and no extra
    journal note or status change occurs.
    """
    e, m, _c, j = _make(mem_fs)
    m.save_evolution_plan(EvolutionPlan(
        plan_id="orig", title="Escalate unlanded remediation to Thomas",
        category="capability",
        rationale="Deliver one merge-or-close message when a lineage stalls past threshold",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    restatement = EvolutionPlan(
        plan_id="dup", title="Escalate unlanded remediation to Thomas once",
        category="capability",
        rationale="Deliver a single merge-or-close message when the lineage stalls past threshold",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )
    await e.submit_for_review(restatement)
    assert len(j.read_events_by_type("evolution_note")) == 1

    # Re-submit the same plan_id — the obsolete row already exists.
    await e.submit_for_review(restatement)  # must not raise
    assert len(j.read_events_by_type("evolution_note")) == 1
    dup = m.get_plan("dup")
    assert dup is not None
    assert dup.status == "obsolete"


@pytest.mark.asyncio
async def test_submit_for_review_blocks_content_restatement_within_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """A differently-titled but content-similar re-proposal of a plan rejected
    inside the cooldown window is refused via the same rejection-cooldown path
    (acceptance criterion 1).
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    fixed_now = _dt(2026, 4, 8, tzinfo=_UTC)
    recent_ts = (fixed_now - _td(days=2)).isoformat()
    rejected = EvolutionPlan(
        plan_id="prej_content", title="Curb Duplicate Proposal Flood",
        category="capability", rationale=_SIMILAR_RATIONALE,
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="wrong module",
        created_at=recent_ts, reviewed_at=recent_ts,
    )
    m.save_evolution_plan(rejected)

    # Different title, near-identical rationale body — the exact-title check
    # cannot catch this, but the content-word Jaccard restatement guard must.
    retry = EvolutionPlan(
        plan_id="pretry_content", title="Refuse Restated Re-proposals",
        category="capability", rationale=_SIMILAR_RATIONALE,
        proposed_changes=["targeted change"], files_affected=[],
        expected_impact="", created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(retry)

    saved = [p for p in m.get_all_plans() if p.plan_id == "pretry_content"]
    assert len(saved) == 0


@pytest.mark.asyncio
async def test_submit_for_review_allows_dissimilar_content_within_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """A candidate below the restatement threshold against every
    within-cooldown rejected plan is still accepted — no false positive
    (acceptance criterion 2).
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    fixed_now = _dt(2026, 4, 8, tzinfo=_UTC)
    recent_ts = (fixed_now - _td(days=2)).isoformat()
    rejected = EvolutionPlan(
        plan_id="prej_dissimilar", title="Curb Duplicate Proposal Flood",
        category="capability", rationale=_SIMILAR_RATIONALE,
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="wrong module",
        created_at=recent_ts, reviewed_at=recent_ts,
    )
    m.save_evolution_plan(rejected)

    # Completely different subject matter — no content-word overlap.
    new_plan = EvolutionPlan(
        plan_id="pnew_dissimilar", title="Speed Dashboard Rendering",
        category="capability",
        rationale=(
            "Dashboard websocket frames render slowly whenever browser tabs "
            "batch repaint events; caching serialized snapshots keeps latency "
            "predictable under load."
        ),
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(new_plan)

    saved = [p for p in m.get_all_plans() if p.plan_id == "pnew_dissimilar"]
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_submit_for_review_allows_content_restatement_outside_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """A content-similar candidate whose only matching rejection is OLDER than
    the cooldown window is still accepted — the window is respected
    (acceptance criterion 3).
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    fixed_now = _dt(2026, 4, 8, tzinfo=_UTC)
    old_ts = (fixed_now - _td(days=10)).isoformat()
    rejected = EvolutionPlan(
        plan_id="prej_content_old", title="Curb Duplicate Proposal Flood",
        category="capability", rationale=_SIMILAR_RATIONALE,
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="wrong module",
        created_at=old_ts, reviewed_at=old_ts,
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry_content_old", title="Refuse Restated Re-proposals",
        category="capability", rationale=_SIMILAR_RATIONALE,
        proposed_changes=["targeted change"], files_affected=[],
        expected_impact="", created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(retry)

    saved = [p for p in m.get_all_plans() if p.plan_id == "pretry_content_old"]
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_submit_for_review_exact_title_refusal_still_fires(
    mem_fs: dict[str, str],
) -> None:
    """Control: the pre-existing exact-title rejection-cooldown refusal path is
    unchanged by the restatement widening (acceptance criterion 1 guard).
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    fixed_now = _dt(2026, 4, 8, tzinfo=_UTC)
    recent_ts = (fixed_now - _td(days=2)).isoformat()
    # Exact same normalized title, but *disjoint* rationale content so only the
    # title path — not the new restatement path — can be responsible.
    rejected = EvolutionPlan(
        plan_id="prej_exact", title="Improve Error Handling",
        category="capability",
        rationale="first attempt wrapping subprocess crashes defensively",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="Too broad",
        created_at=recent_ts, reviewed_at=recent_ts,
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry_exact", title="Improve Error Handling",
        category="capability",
        rationale="totally unrelated payload compression tuning knob work",
        proposed_changes=["targeted change"], files_affected=[],
        expected_impact="", created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(retry)

    saved = [p for p in m.get_all_plans() if p.plan_id == "pretry_exact"]
    assert len(saved) == 0
