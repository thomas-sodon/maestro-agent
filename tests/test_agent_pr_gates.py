"""Tests for the dequeue-face PR-remediation gates on ``MaestroAgent``.

Covers the costume-signature gate ``_gate_pr_remediation_branch_resolved``,
which zero-cost-skips a PR that has already been resolved-on-branch ≥N times
without ever landing — the agent's documented "awaiting tend push" costume —
and escalates once instead of re-lapping the wheel.

Also covers the marker-centralization invariant (proposal 070, step 1):
the four on-disk protocol marker literals must appear only in
``maestro/pr_remediation_state.py``; every other module uses ``Marker.*``.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.loop.pr_remediation import PrRemediationController
from maestro.memory import TaskRecord
from maestro.pr_remediation_state import (
    Marker,
    PrRemediationDisposition,
    PrRemediationState,
    classify_pr_remediation_disposition,
)


def _seed_branch_resolved(
    agent,  # type: ignore[no-untyped-def]
    pr_number: int,
    count: int,
) -> None:
    """Record *count* ``partial`` branch-resolved laps for *pr_number*."""
    for i in range(count):
        agent.memory.record_task(TaskRecord(
            task_id=f"task{pr_number}{i}",
            goal=f"Remediate PR #{pr_number}: fix CI",
            plan=[],
            outcome="partial",
            result_summary="Resolved on branch, awaiting tend push",
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source="external",
            key_findings="Resolved on branch, awaiting tend push",
        ))


class TestBranchResolvedGate:
    """``_gate_pr_remediation_branch_resolved`` — costume-signature skip."""

    @pytest.mark.asyncio
    async def test_gate_fires_at_threshold(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        # Threshold default is 2 — seed exactly two branch-resolved laps.
        _seed_branch_resolved(agent, 2531, 2)

        with patch("maestro.agent.query_on_session") as query:
            gated = await agent._gate_pr_remediation_branch_resolved(
                "Remediate PR #2531: fix CI", "maestro/evolve/ep2531",
                "external",
            )

        assert gated is True
        # No SDK session was opened.
        query.assert_not_called()
        # A zero-cost ``partial`` no-op stamped ``[branch-resolved]`` was
        # recorded — never ``success``.
        tasks = agent.memory.get_recent_tasks(10)
        skip = tasks[-1]
        assert skip.outcome == "partial"
        assert skip.duration_secs == 0.0
        assert "[branch-resolved]" in (skip.key_findings or "")
        # Escalation fired exactly once and names the PR.
        agent._notify_best_effort.assert_awaited_once()
        message = agent._notify_best_effort.await_args.args[0]
        assert "PR #2531" in message

    @pytest.mark.asyncio
    async def test_gate_fires_above_threshold(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        _seed_branch_resolved(agent, 2538, 4)

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2538: fix CI", "maestro/evolve/ep2538", "external",
        )

        assert gated is True
        agent._notify_best_effort.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gate_passes_below_threshold(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        # Only one prior lap — below the default threshold of 2.
        _seed_branch_resolved(agent, 2531, 1)

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2531: fix CI", "maestro/evolve/ep2531", "external",
        )

        assert gated is False
        # Below threshold: nothing recorded, nothing escalated.
        agent._notify_best_effort.assert_not_awaited()
        assert len(agent.memory.get_recent_tasks(10)) == 1

    @pytest.mark.asyncio
    async def test_disabled_flag_bypasses_gate(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent.config.branch_resolved_gate_enabled = False
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        # Plenty of laps, but the gate is disabled — it must pass through.
        _seed_branch_resolved(agent, 2531, 5)

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2531: fix CI", "maestro/evolve/ep2531", "external",
        )

        assert gated is False
        agent._notify_best_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unparseable_goal_passes_through(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Build a new sensor", None, "self_directed",
        )

        assert gated is False
        agent._notify_best_effort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_stamp_not_counted_as_false_success(
        self, make_agent,  # type: ignore[no-untyped-def]
    ) -> None:
        # The skip records ``partial``, so a later sensor reading the history
        # never sees a fabricated ``success`` for this PR.
        from dataclasses import asdict

        from maestro.audit_checks import measure_false_success_recurrence

        agent = make_agent()
        agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
        _seed_branch_resolved(agent, 2531, 2)

        gated = await agent._gate_pr_remediation_branch_resolved(
            "Remediate PR #2531: fix CI", "maestro/evolve/ep2531", "external",
        )
        assert gated is True

        history = [asdict(t) for t in agent.memory.get_recent_tasks(10)]
        # No record for #2531 carries a ``success`` stamp.
        result = measure_false_success_recurrence(history, n=0)
        assert result == {"count": 0, "by_pr": {}}


# ---------------------------------------------------------------------------
# Proposal 070 step 1 — marker-centralization invariant
# ---------------------------------------------------------------------------

_MAESTRO_PKG = Path(__file__).parent.parent / "maestro"
_CANONICAL_FILE = _MAESTRO_PKG / "pr_remediation_state.py"
# The four on-disk protocol literals that must live only in pr_remediation_state.py.
_MARKER_LITERALS = {str(m) for m in Marker}


def _string_constants(tree: ast.AST) -> list[str]:
    """Return every string constant value in *tree* (AST, not raw source)."""
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
        # Python 3.11 f-strings expose FormattedValue children; the static
        # parts are JoinedStr / Constant nodes already covered by ast.walk.
    return values


def _code_string_constants(tree: ast.AST) -> list[str]:
    """Return code-level string constants in *tree*, excluding docstrings.

    Unlike :func:`_string_constants`, this drops bare string-literal statements
    (docstrings/prose) so the marker scan can match marker literals as
    *substrings* — catching a literal re-inlined into an f-string
    (``f"[merge-block] ..."`` → static part ``"[merge-block] "``) or a
    concatenation (``"[merge-block] " + x``) — without tripping on docstrings
    that name a marker in prose. A docstring is a bare string-literal statement
    (a string ``Expr``): the leading string of a module/class/function body or
    a free-standing reST attribute docstring. f-string static parts are ordinary
    ``Constant`` children of ``JoinedStr`` (never bare ``Expr`` statements), so
    they are kept. A single fused ``ast.walk`` keeps this cheap for the
    whole-package scan in :meth:`TestMarkerCentralization`: ``ast.walk`` is
    breadth-first, so a docstring ``Expr`` is always yielded before its
    ``Constant`` child, and recording the docstring id first lets the same pass
    skip it.
    """
    docstring_ids: set[int] = set()
    values: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            docstring_ids.add(id(node.value))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            values.append(node.value)
    return values


class TestMarkerCentralization:
    """The four marker literals must appear only in pr_remediation_state.py."""

    def test_literals_absent_from_all_other_source_files(self) -> None:
        violators: list[str] = []
        for path in sorted(_MAESTRO_PKG.rglob("*.py")):
            if path == _CANONICAL_FILE:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            # Substring match (not exact equality) so a literal re-inlined into
            # a larger f-string / concatenation is caught, not just a bare
            # constant. Docstrings are excluded so prose mentions don't false-flag.
            for value in _code_string_constants(tree):
                hit = next((lit for lit in _MARKER_LITERALS if lit in value), None)
                if hit is not None:
                    rel = path.relative_to(_MAESTRO_PKG.parent)
                    violators.append(f"{rel}: {hit!r} embedded in {value!r}")
        assert violators == [], (
            "Marker literals found outside pr_remediation_state.py — "
            "use Marker.* instead:\n" + "\n".join(violators)
        )

    def test_inline_fstring_literal_is_detected(self) -> None:
        # Regression for the gap the AI review flagged: a developer re-inlining
        # a raw literal into an f-string yields a static part ("[merge-block] PR
        # #") that is NOT exactly a marker value, so the old exact-equality scan
        # passed while the single-definition-site invariant was violated.
        source = 'msg = f"[merge-block] PR #{pr_num} is behind main"\n'
        tree = ast.parse(source)
        values = _code_string_constants(tree)
        hits = [v for v in values if any(lit in v for lit in _MARKER_LITERALS)]
        assert hits == ["[merge-block] PR #"]

    def test_inline_concatenation_literal_is_detected(self) -> None:
        # The concatenation form ("[branch-resolved] " + reason) leaks the same
        # way and must also be caught by substring matching.
        source = 'msg = "[branch-resolved] " + reason\n'
        tree = ast.parse(source)
        values = _code_string_constants(tree)
        hits = [v for v in values if any(lit in v for lit in _MARKER_LITERALS)]
        assert hits == ["[branch-resolved] "]

    def test_docstring_literal_mention_is_not_flagged(self) -> None:
        # A docstring (or any bare string statement) naming a marker in prose is
        # legitimate documentation, not a call-site leak — it must not be flagged.
        source = (
            '"""Documents the [merge-block] / awaiting tend push markers."""\n'
            "x = 1\n"
        )
        tree = ast.parse(source)
        values = _code_string_constants(tree)
        hits = [v for v in values if any(lit in v for lit in _MARKER_LITERALS)]
        assert hits == []

    def test_marker_values_are_byte_identical_to_legacy_literals(self) -> None:
        # ``==`` confirms the value, but every write path persists a marker via
        # ``str()`` / f-string interpolation (e.g. ``f"{Marker.MERGE_BLOCK} PR
        # #{n} ..."``), so the on-disk bytes are determined by the *rendering*,
        # not equality. ``StrEnum`` makes the two coincide today; pinning the
        # rendering here catches a future base-class regression to a plain
        # ``class Marker(str, Enum)``, where ``==`` would still pass but
        # ``f"{Marker.MERGE_BLOCK}"`` would render ``"Marker.MERGE_BLOCK"`` and
        # silently corrupt every persisted marker.
        for marker, literal in (
            (Marker.MERGE_BLOCK, "[merge-block]"),
            (Marker.READY, "[ready]"),
            (Marker.PREFLIGHT, "[preflight]"),
            (Marker.BRANCH_RESOLVED, "[branch-resolved]"),
            (Marker.AWAITING_TEND_PUSH, "awaiting tend push"),
        ):
            assert marker == literal
            assert str(marker) == literal
            assert f"{marker}" == literal

    def test_canonical_file_defines_all_four_markers(self) -> None:
        source = _CANONICAL_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_CANONICAL_FILE))
        found = set(_string_constants(tree))
        for m in Marker:
            assert str(m) in found, f"Marker {m!r} missing from {_CANONICAL_FILE.name}"


# ---------------------------------------------------------------------------
# Proposal 080 step 5 (Phase 4) — unified dequeue disposition classifier
# ---------------------------------------------------------------------------

# The intended state → disposition grouping. ``RUN`` is reachable from
# ``REMEDIABLE`` alone so the disposition-derived run-vs-skip boolean is
# byte-identical to the legacy ``state is not REMEDIABLE`` comparison.
_EXPECTED_DISPOSITION = {
    PrRemediationState.MERGED: PrRemediationDisposition.EVICT_MERGED,
    PrRemediationState.LANDED: PrRemediationDisposition.EVICT_MERGED,
    PrRemediationState.PREFLIGHT_CLEAN: PrRemediationDisposition.EVICT_MERGED,
    PrRemediationState.HOST_CLOSED_UNMERGED: (
        PrRemediationDisposition.EVICT_MERGED
    ),
    PrRemediationState.MERGE_BLOCKED: PrRemediationDisposition.TEND_WILL_CLEAR,
    PrRemediationState.READY_AND_WAITING: PrRemediationDisposition.HANDOFF,
    PrRemediationState.STOOD_DOWN: PrRemediationDisposition.HANDOFF,
    PrRemediationState.BRANCH_RESOLVED: PrRemediationDisposition.HANDOFF,
    PrRemediationState.REPETITION_EXHAUSTED: PrRemediationDisposition.HANDOFF,
    PrRemediationState.REMEDIABLE: PrRemediationDisposition.RUN,
}


class TestDispositionClassifier:
    """``classify_pr_remediation_disposition`` — the single decision site."""

    def test_every_state_maps_to_expected_disposition(self) -> None:
        # Exhaustive over the enum: a future PrRemediationState added without a
        # disposition fails here (and KeyErrors at runtime) rather than silently
        # defaulting to run-or-skip. Pins the intended grouping, too.
        for state in PrRemediationState:
            assert (
                classify_pr_remediation_disposition(state)
                is _EXPECTED_DISPOSITION[state]
            )

    def test_run_is_reachable_only_from_remediable(self) -> None:
        # The load-bearing parity invariant: exactly one state maps to RUN, and
        # it is REMEDIABLE — so deriving run-vs-skip from the disposition equals
        # the legacy ``state is not REMEDIABLE`` comparison for every state.
        runnable = [
            s
            for s in PrRemediationState
            if classify_pr_remediation_disposition(s)
            is PrRemediationDisposition.RUN
        ]
        assert runnable == [PrRemediationState.REMEDIABLE]

    def test_mapping_is_total_no_keyerror(self) -> None:
        # No state raises — the mapping covers the whole enum.
        for state in PrRemediationState:
            classify_pr_remediation_disposition(state)


class TestDequeueRouting:
    """``dequeue_gates`` evicts via the single disposition decision site."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", list(PrRemediationState))
    async def test_evicts_exactly_when_not_remediable(
        self, state: PrRemediationState,
    ) -> None:
        """For every classified state, ``dequeue_gates`` evicts (returns
        ``True``) exactly when the state is not ``REMEDIABLE`` — the boolean is
        now derived solely through ``classify_pr_remediation_disposition``, and
        ``RUN`` maps from ``REMEDIABLE`` alone. ``classify_dequeue`` is walked
        exactly once, so the gates' side effects (the zero-cost ``TaskRecord``,
        the escalation notify) that live inside the walk run once, not twice.
        """
        entry = PendingPRRequest(
            directive="Remediate PR #7: fix CI", pr_branch="b7",
        )
        # ``dequeue_gates`` reads nothing off the agent once ``classify_dequeue``
        # is patched to a fixed state — it derives its boolean purely from the
        # disposition classifier — so an empty namespace is all it needs.
        ctrl = PrRemediationController(
            SimpleNamespace(),  # type: ignore[arg-type]
        )
        ctrl.classify_dequeue = AsyncMock(  # type: ignore[method-assign]
            return_value=state,
        )
        gated = await ctrl.dequeue_gates(
            goal=entry.directive,
            pr_branch="b7",
            source="external",
            entry=entry,
        )
        ctrl.classify_dequeue.assert_awaited_once()
        expected_evict = state is not PrRemediationState.REMEDIABLE
        assert gated is expected_evict
