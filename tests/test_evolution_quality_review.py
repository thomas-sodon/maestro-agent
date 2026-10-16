"""auto_fix, long-context self_review, uncalled-symbol detection and the
runnability verdict (``maestro/evolution_quality.py``); split out of
``tests/test_evolution.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_quality, gate_environment
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    _PLAN_CHANGE_ITEM_MAX_CHARS,
    _PLAN_IMPACT_MAX_CHARS,
    _PLAN_RATIONALE_MAX_CHARS,
    _PLAN_TITLE_MAX_CHARS,
    UNPARSEABLE_PLAN_PLACEHOLDER,
    EvolutionEngine,
)
from maestro.evolution_submission import (
    _extract_added_cli_surface,
)
from maestro.journal import Journal
from maestro.memory import (
    EvolutionPlan,
    Memory,
)
from maestro.notifications.base import NullNotifier
from tests.conftest import MockQueryResult, arm_entry_clean_tree
from tests.test_evolution import (
    _FAIL_REVIEW,
    _make,
    _make_engine,
    _observing_engine,
)

# ---------------------------------------------------------------------------
# Merged from bug hunt files — propose_plan edge cases
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(mem_fs: dict[str, str]) -> EvolutionEngine:
    d = Path("/fake/evo_deep")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    memory = Memory(d)
    consciousness = Consciousness(d)
    journal = Journal(d)
    notifier = NullNotifier()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.has_changes.return_value = True
    vcs.get_head_hash.return_value = "abc123"
    code_host = MagicMock()
    return EvolutionEngine(
        memory, consciousness, journal, notifier,
        config=config, vcs=vcs, code_host=code_host,
    )


@pytest.mark.asyncio
async def test_implement_passes_tool_stream_idle_timeout(
    mem_fs: dict[str, str],
) -> None:
    """implement() must pass the tool-caller idle timeout on both paths."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(sdk_tool_stream_idle_timeout_seconds=777.0)
    plan = EvolutionPlan(
        plan_id="pTimeout", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)
    assert mock_q.call_args.kwargs["stream_idle_timeout_seconds"] == 777.0

    vcs.reset_mock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    session = MagicMock()
    with patch(
        "maestro.evolution.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response="done")
        await e.implement(plan, session=session)
    assert mock_qos.call_args.kwargs["stream_idle_timeout_seconds"] == 777.0


@pytest.mark.asyncio
async def test_implement_pins_cwd_to_workspace(mem_fs: dict[str, str]) -> None:
    """implement()'s bare query must pin the agent to the workspace tree.

    Without ``cwd`` the SDK agent subprocess edits whatever directory the
    loop process was launched from while the surrounding git plumbing
    targets ``config.workspace`` — issue #420.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="implCwd", title="Cwd test",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "abc123"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        await e.implement(plan)

    options = mock_q.call_args.kwargs["options"]
    assert options.cwd == str(e._config.workspace)


def test_self_review_is_public_api() -> None:
    """``self_review`` is public — it is called cross-class from ``MaestroAgent``.

    A leading-underscore name advertises "internal, do not call from elsewhere"
    for a method that is only ever invoked from *outside* the class (the
    self-review step was moved to the caller so it shares the implementation
    session).  This pins the encapsulation contract so a future refactor cannot
    silently re-privatize a method that is part of the inter-class contract.
    Pure attribute introspection — no I/O, no mocks, no filesystem.
    """
    assert hasattr(EvolutionEngine, "self_review") is True
    assert hasattr(EvolutionEngine, "_self_review") is False


@pytest.mark.asyncio
async def test_self_review_passes_tool_stream_idle_timeout(
    mem_fs: dict[str, str],
) -> None:
    """self_review() must pass the tool-caller idle timeout on both paths."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        sdk_tool_stream_idle_timeout_seconds=321.0,
        self_review_max_rounds=1,
    )
    plan = EvolutionPlan(
        plan_id="pSR", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff text", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")
    assert mock_q.call_args.kwargs["stream_idle_timeout_seconds"] == 321.0

    session = MagicMock()
    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch(
             "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
         ) as mock_qos:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff text", stderr="",
        )
        mock_qos.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base", session=session)
    assert mock_qos.call_args.kwargs["stream_idle_timeout_seconds"] == 321.0


async def _grab_self_review_prompt(
    mem_fs: dict[str, str], *, require_claude_md_update: bool,
) -> str:
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        self_review_max_rounds=1,
        require_claude_md_update=require_claude_md_update,
    )
    plan = EvolutionPlan(
        plan_id="pSRc", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    e._vcs = vcs
    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff text", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")
    return str(mock_q.call_args.kwargs["prompt"])


@pytest.mark.asyncio
async def test_self_review_claude_md_checklist_item_is_flag_gated(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 108: the self_review checklist's CLAUDE.md item is flag-gated.

    With the default (``require_claude_md_update`` False) the review checklist
    omits the "5. CLAUDE.md — is it updated…" item entirely; with the flag on
    it reappears.
    """
    off = await _grab_self_review_prompt(mem_fs, require_claude_md_update=False)
    assert "is it updated if architecture changed" not in off

    on = await _grab_self_review_prompt(mem_fs, require_claude_md_update=True)
    assert "**CLAUDE.md** — is it updated if architecture changed" in on


@pytest.mark.asyncio
async def test_self_review_pins_cwd_to_workspace(mem_fs: dict[str, str]) -> None:
    """self_review()'s bare query must pin the agent to the workspace tree.

    Without ``cwd`` the SDK agent subprocess edits whatever directory the
    loop process was launched from while the surrounding git plumbing
    targets ``config.workspace`` — issue #420.
    """
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="srCwd", title="Cwd test",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff text", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    options = mock_q.call_args.kwargs["options"]
    assert options.cwd == str(e._config.workspace)


@pytest.mark.asyncio
async def test_auto_fix_passes_tool_stream_idle_timeout(
    mem_fs: dict[str, str],
) -> None:
    """auto_fix() must pass the tool-caller idle timeout on both paths."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(sdk_tool_stream_idle_timeout_seconds=456.0)
    plan = EvolutionPlan(
        plan_id="pAF", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)
    assert mock_q.call_args.kwargs["stream_idle_timeout_seconds"] == 456.0

    session = MagicMock()
    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch(
             "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
         ) as mock_qos:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_qos.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(
            plan, _FAIL_REVIEW, session=session, round_n=1, max_rounds=3,
        )
    assert mock_qos.call_args.kwargs["stream_idle_timeout_seconds"] == 456.0


@pytest.mark.asyncio
async def test_auto_fix_applies_changes(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="af1", title="Auto fix test",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff output", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="fixed")

        result = await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    assert result is True
    vcs.stage_all.assert_called_once()
    vcs.amend_commit.assert_called_once()
    # Verify the prompt includes the review output
    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "E501" in prompt
    # auto_fix is a tool-using query that runs unbounded; the deterministic
    # per-call ceiling is the SDK budget cap, not a turn count.
    options = mock_q.call_args.kwargs["options"]
    assert options.max_turns is None


@pytest.mark.asyncio
async def test_auto_fix_no_changes_returns_false(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="af2", title="No-change fix test",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = False
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff output", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="nothing to fix")

        result = await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    assert result is False
    vcs.stage_all.assert_not_called()
    vcs.amend_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Long-context opt-in (proposal-derived: targeted 1M-context at three sites)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_review_long_diff_uses_long_context_model(
    mem_fs: dict[str, str],
) -> None:
    """When sdk_model_long_context is set, self_review widens the diff cap and
    flips model + betas on the QueryOptions."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        sdk_model_long_context="claude-opus-4-7[1m]",
        self_review_max_rounds=1,
    )
    plan = EvolutionPlan(
        plan_id="pLC", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    big_diff = "diff " * 50_000  # ~250 KB synthetic diff (over the 50K cap)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = big_diff
    e._vcs = vcs

    with patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    options = mock_q.call_args.kwargs["options"]
    assert options.model == "claude-opus-4-7[1m]"
    assert options.betas == ["context-1m-2025-08-07"]

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    # The standard cap would have left ~50K chars in the prompt; the
    # long-context cap (500K) leaves the full ~250 KB diff intact.
    assert len(sent_prompt) > 200_000


@pytest.mark.asyncio
async def test_self_review_default_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """With long-context off, the existing 50K cap and bare model still apply."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)  # long_context = ""
    plan = EvolutionPlan(
        plan_id="pNo", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    big_diff = "diff " * 50_000
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = big_diff
    e._vcs = vcs

    with patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    options = mock_q.call_args.kwargs["options"]
    assert options.model is None
    assert options.betas is None

    # Standard 50K cap means the prompt holds at most ~50K of diff
    sent_prompt = mock_q.call_args.kwargs["prompt"]
    assert len(sent_prompt) < 70_000
    assert "truncated by self-review diff cap" in sent_prompt


@pytest.mark.asyncio
async def test_self_review_session_path_ignores_long_context(
    mem_fs: dict[str, str],
) -> None:
    """Session-reuse path keeps the 50K cap when the implementation flag
    is *off* — the session inherits the standard model in that case,
    so widening the diff would just truncate at the model's window
    instead.  The cap only opens when ``long_context_implementation_enabled``
    is set, signalling the impl session itself was created on long-context."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        sdk_model_long_context="claude-opus-4-7[1m]",
        # long_context_implementation_enabled defaults to False
        self_review_max_rounds=1,
    )
    plan = EvolutionPlan(
        plan_id="pSess", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    big_diff = "diff " * 50_000
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = big_diff
    e._vcs = vcs

    session = MagicMock()
    with patch(
        "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base", session=session)

    # Session path: query_on_session does not take a model/betas
    # override — the diff cap stays at 50K so the prompt is bounded.
    sent_prompt = mock_qos.call_args.args[1]
    assert len(sent_prompt) < 70_000


@pytest.mark.asyncio
async def test_self_review_session_path_long_context_widens_cap(
    mem_fs: dict[str, str],
) -> None:
    """When ``long_context_implementation_enabled`` is set, Session C
    is created on the long-context model in ``agent.implement_plan``
    and the self-review running on that session must expand its diff
    cap to match — otherwise the 1M window the session already holds
    is wasted on a 50K-truncated review.  ``query_on_session`` inherits
    model + betas from the session, so the session path does not
    re-attach those — only the cap broadens."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(
        sdk_model_long_context="claude-opus-4-7[1m]",
        long_context_implementation_enabled=True,
        self_review_max_rounds=1,
    )
    plan = EvolutionPlan(
        plan_id="pSessLC", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    big_diff = "diff " * 50_000  # ~250 KB synthetic diff (over the 50K cap)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = big_diff
    e._vcs = vcs

    session = MagicMock()
    with patch(
        "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base", session=session)

    # Session path under long-context impl: prompt holds the full diff,
    # not the 50K-truncated head+tail.
    sent_prompt = mock_qos.call_args.args[1]
    assert len(sent_prompt) > 200_000
    assert "truncated by self-review diff cap" not in sent_prompt


@pytest.mark.asyncio
async def test_self_review_includes_acceptance_criteria_block(
    mem_fs: dict[str, str],
) -> None:
    """When acceptance_criteria is non-empty, the review prompt must contain
    the binding-citation block and the per-criterion checklist item — the
    contract the proposal is built around."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="pAC", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        acceptance_criteria=[
            "criterion alpha",
            "criterion beta",
        ],
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    with patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Acceptance Criteria (binding)" in sent_prompt
    assert "1. criterion alpha" in sent_prompt
    assert "2. criterion beta" in sent_prompt
    assert "MUST cite" in sent_prompt
    assert "diff hunk" in sent_prompt
    assert "test asserting it" in sent_prompt
    # Flag off, no uncalled/hollow: acceptance criteria is the sole optional
    # item, so computed numbering places it at item 5 (right after Tests).
    assert "5. **Acceptance criteria**" in sent_prompt


@pytest.mark.asyncio
async def test_self_review_omits_block_for_legacy_plan(
    mem_fs: dict[str, str],
) -> None:
    """A legacy plan with no acceptance_criteria gets the original review
    prompt — no binding block, no acceptance-criteria checklist item — so
    existing PR-tending and legacy-plan paths stay silent."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="pLegacy", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    assert plan.acceptance_criteria == []
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    with patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Acceptance Criteria (binding)" not in sent_prompt
    assert "**Acceptance criteria**" not in sent_prompt


@pytest.mark.asyncio
async def test_self_review_always_includes_minimalism_item(
    mem_fs: dict[str, str],
) -> None:
    """The minimalism / anti-reinvention checklist item is always-on: it must
    reach the review prompt even for a legacy plan with no acceptance_criteria
    and a diff that triggers neither the uncalled-symbol nor hollow-artifact
    conditional items — the backstop against overbuilding is unconditional.
    """
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="pMin", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    assert plan.acceptance_criteria == []
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    with patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    # No conditional item fired for this legacy plan / trivial diff …
    assert "**Acceptance criteria**" not in sent_prompt
    assert "**Uncalled symbols**" not in sent_prompt
    assert "**Hollow artifacts**" not in sent_prompt
    # … yet the minimalism backstop is present unconditionally. Proposal 108's
    # computed gap-free numbering places it right after the four always-on
    # items (item 5) when nothing conditional fires, not at a fixed 9.
    assert "5. **Minimalism**" in sent_prompt
    assert "smallest diff that satisfies the plan" in sent_prompt
    # … and it treats a net-subtractive diff as satisfying, not suspect.
    assert "net-subtractive diff" in sent_prompt


@pytest.mark.asyncio
async def test_self_review_failure_replay_forces_partials_citation(
    mem_fs: dict[str, str],
) -> None:
    """Heart-of-plan regression test: synthesize the historical task_cost_gate
    rule-3 plan and a diff that reverts to the failed shape (failure-only).
    Assert the generated review prompt forces a per-criterion citation that
    explicitly mentions ``partial`` outcomes — the contract a future LLM
    that emits LGTM with the failure-only shape would visibly violate."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="pRule3", title="task_cost_gate rule 3 cost ratio",
        category="capability",
        rationale="Partials are 2.2x the cost of successes; rule 3 must "
                  "include them in the failure-cost numerator.",
        proposed_changes=[
            "Update _failure_cost_ratio to count both failure and partial outcomes",
        ],
        files_affected=["maestro/task_cost_gate.py"],
        expected_impact="Closer to safe self-direction",
        acceptance_criteria=[
            "task_cost_gate rule 3 counts both `failure` and `partial` "
            "outcomes in the failure-cost numerator.",
        ],
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    reverted_diff = (
        "diff --git a/maestro/task_cost_gate.py b/maestro/task_cost_gate.py\n"
        "@@\n"
        "-    bad_secs = sum(t.duration_secs for t in tasks "
        "if t.outcome in (\"failure\", \"partial\"))\n"
        "+    bad_secs = sum(t.duration_secs for t in tasks "
        "if t.outcome == \"failure\")\n"
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = reverted_diff
    e._vcs = vcs

    with patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Acceptance Criteria (binding)" in sent_prompt
    assert "partial" in sent_prompt
    assert "MUST cite" in sent_prompt
    assert "test asserting it" in sent_prompt
    # Flag off, no uncalled/hollow: acceptance criteria is item 5.
    assert "5. **Acceptance criteria**" in sent_prompt


# ---------------------------------------------------------------------------
# Uncalled-symbol detection — self_review integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_review_injects_uncalled_symbol_warnings(
    mem_fs: dict[str, str],
) -> None:
    """When _find_uncalled_symbols returns warnings, the review prompt must
    contain the ## Uncalled Symbol Warnings block and the uncalled-symbols
    checklist item (item 5 here: flag off, no criteria, no hollow)."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="pUS", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    fake_warnings = [
        "maestro/foo.py: symbol 'bar' has no external call sites "
        "outside its definition file and tests/",
    ]

    with patch.object(e, "_find_uncalled_symbols", return_value=fake_warnings), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Uncalled Symbol Warnings" in sent_prompt
    assert "symbol 'bar'" in sent_prompt
    assert "5. **Uncalled symbols**" in sent_prompt
    assert "documented reason for being unused" in sent_prompt


@pytest.mark.asyncio
async def test_self_review_no_uncalled_symbols_omits_block(
    mem_fs: dict[str, str],
) -> None:
    """When _find_uncalled_symbols returns an empty list, neither the
    warnings block nor the uncalled-symbols checklist item should appear."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="pNoUS", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    with patch.object(e, "_find_uncalled_symbols", return_value=[]), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Uncalled Symbol Warnings" not in sent_prompt
    assert "**Uncalled symbols**" not in sent_prompt


@pytest.mark.asyncio
async def test_self_review_uncalled_block_position(
    mem_fs: dict[str, str],
) -> None:
    """The uncalled-symbol block must appear between the diff and the
    review instructions — after the closing ``` and before ## Review."""
    e, _m, _c, _j = _make(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = EvolutionPlan(
        plan_id="pPos", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "deadbeef"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    fake_warnings = ["maestro/x.py: symbol 'orphan' has no external call sites"]

    with patch.object(e, "_find_uncalled_symbols", return_value=fake_warnings), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    sent_prompt = mock_q.call_args.kwargs["prompt"]
    diff_end = sent_prompt.index("```\n", sent_prompt.index("```diff"))
    uncalled_start = sent_prompt.index("## Uncalled Symbol Warnings")
    review_start = sent_prompt.index("## Review Instructions")
    assert diff_end < uncalled_start < review_start


# ------------------------------------------------------------------
# _find_uncalled_symbols — unit tests
# ------------------------------------------------------------------

_SAMPLE_DIFF = """\
diff --git a/maestro/evolution.py b/maestro/evolution.py
index abc1234..def5678 100644
--- a/maestro/evolution.py
+++ b/maestro/evolution.py
@@ -10,0 +11,6 @@
+def new_helper(x):
+    return x + 1
+
+class NewWidget:
+    pass
"""

_SAMPLE_DIFF_DUNDER = """\
diff --git a/maestro/foo.py b/maestro/foo.py
--- a/maestro/foo.py
+++ b/maestro/foo.py
@@ -1,0 +2,4 @@
+    def __init__(self):
+        pass
+    def __repr__(self):
+        return "Foo"
"""

_SAMPLE_DIFF_TEST_HELPER = """\
diff --git a/maestro/bar.py b/maestro/bar.py
--- a/maestro/bar.py
+++ b/maestro/bar.py
@@ -1,0 +2,2 @@
+def _test_setup():
+    pass
"""

_SAMPLE_DIFF_ASYNC_DEF = """\
diff --git a/maestro/async_mod.py b/maestro/async_mod.py
--- a/maestro/async_mod.py
+++ b/maestro/async_mod.py
@@ -1,0 +2,3 @@
+async def fetch_data():
+    pass
"""


def test_find_uncalled_symbols_all_uncalled(mem_fs: dict[str, str]) -> None:
    """Symbols with zero grep matches should each produce a warning."""
    engine = _make_engine(mem_fs)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        # git grep returns exit-code 1 → no matches
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF)

    assert len(result) == 2
    assert any("new_helper" in w for w in result)
    assert any("NewWidget" in w for w in result)
    # Both should reference the definition file
    for w in result:
        assert "maestro/evolution.py" in w


def test_find_uncalled_symbols_all_called(mem_fs: dict[str, str]) -> None:
    """Symbols found by grep should NOT produce warnings."""
    engine = _make_engine(mem_fs)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        # One batched git grep -n returns a call site for each symbol in a
        # different file → both are real usages.
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout=(
                "maestro/agent.py:5:    new_helper(x)\n"
                "maestro/agent.py:9:    NewWidget()\n"
            ),
            stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF)

    assert result == []


def test_find_uncalled_symbols_skips_dunders(mem_fs: dict[str, str]) -> None:
    """Dunder methods like __init__ and __repr__ must be skipped."""
    engine = _make_engine(mem_fs)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF_DUNDER)

    # No grep calls should have been made — all symbols are dunders
    assert calls == []
    assert result == []


def test_find_uncalled_symbols_skips_test_helpers(mem_fs: dict[str, str]) -> None:
    """Private test helpers (names starting with _test) must be skipped."""
    engine = _make_engine(mem_fs)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF_TEST_HELPER)

    assert calls == []
    assert result == []


def test_find_uncalled_symbols_async_def(mem_fs: dict[str, str]) -> None:
    """async def should be detected as a new symbol."""
    engine = _make_engine(mem_fs)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF_ASYNC_DEF)

    assert len(result) == 1
    assert "fetch_data" in result[0]
    assert "maestro/async_mod.py" in result[0]


def test_find_uncalled_symbols_empty_diff(mem_fs: dict[str, str]) -> None:
    """An empty diff should produce no warnings."""
    engine = _make_engine(mem_fs)

    with patch("subprocess.run") as mock_run:
        result = engine._find_uncalled_symbols("")

    mock_run.assert_not_called()
    assert result == []


def test_find_uncalled_symbols_grep_timeout(mem_fs: dict[str, str]) -> None:
    """Subprocess timeouts should be handled gracefully (symbol skipped)."""
    engine = _make_engine(mem_fs)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout=30)

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF)

    # Timeout → skip, not crash; no warnings produced
    assert result == []


def test_find_uncalled_symbols_grep_oserror(mem_fs: dict[str, str]) -> None:
    """OSError from subprocess should be handled gracefully."""
    engine = _make_engine(mem_fs)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git not found")

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF)

    assert result == []


def test_find_uncalled_symbols_mixed(mem_fs: dict[str, str]) -> None:
    """One symbol called externally, one not — only the uncalled one warns.

    With a single batched grep the stdout carries rows for both symbols;
    attribution is by the per-symbol ``name in content`` substring filter, so
    ``new_helper``'s external call site does not count for ``NewWidget``.
    """
    engine = _make_engine(mem_fs)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        # One batched grep: new_helper has an external call site, NewWidget
        # appears only on its own definition line.
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout=(
                "maestro/agent.py:5:    new_helper(x)\n"
                "maestro/evolution.py:14:class NewWidget:\n"
            ),
            stderr="",
        )

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(_SAMPLE_DIFF)

    assert len(result) == 1
    assert "NewWidget" in result[0]


def test_find_uncalled_symbols_grep_command_shape(
    mem_fs: dict[str, str],
) -> None:
    """git grep must run *once* for all symbols (#1856), search line-numbered
    (-n), carry one ``-e <name>`` per symbol, exclude tests/, and — after
    #2410 — NOT exclude the definition file (intra-module call sites count;
    only the definition line is filtered in-process)."""
    engine = _make_engine(mem_fs)

    captured_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        captured_cmds.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr=""
        )

    with patch("subprocess.run", side_effect=fake_run):
        engine._find_uncalled_symbols(_SAMPLE_DIFF)

    # A single batched invocation for the two symbols, not one per symbol.
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "-n" in cmd
    assert ":!tests/" in cmd
    assert ":!maestro/evolution.py" not in cmd
    # One ``-e <name>`` per symbol in the single command.
    assert cmd.count("-e") == 2
    assert "new_helper" in cmd
    assert "NewWidget" in cmd


def test_find_uncalled_symbols_no_file_header(mem_fs: dict[str, str]) -> None:
    """Added symbols before any file header should be ignored."""
    diff = """\
@@ -1,0 +2,2 @@
+def orphan_func():
+    pass
"""
    engine = _make_engine(mem_fs)

    with patch("subprocess.run") as mock_run:
        result = engine._find_uncalled_symbols(diff)

    mock_run.assert_not_called()
    assert result == []


def test_find_uncalled_symbols_context_lines_ignored(
    mem_fs: dict[str, str],
) -> None:
    """Lines starting with ++ (diff header) must not be treated as additions."""
    diff = """\
diff --git a/maestro/x.py b/maestro/x.py
--- a/maestro/x.py
+++ b/maestro/x.py
@@ -1,0 +2,2 @@
+def real_addition():
+    pass
"""
    engine = _make_engine(mem_fs)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = engine._find_uncalled_symbols(diff)

    # Only real_addition, NOT "b/maestro/x.py" (from the +++ line)
    assert len(result) == 1
    assert "real_addition" in result[0]


# ---------------------------------------------------------------------------
# collect_suite — the runnability oracle's collector adapter (proposal 133)
# ---------------------------------------------------------------------------


def test_collect_suite_returns_the_finished_run_triple(
    mem_fs: dict[str, str],
) -> None:
    """A run that finished is reported as its whole (rc, stdout, stderr) triple."""
    e = _make_engine(mem_fs)
    finished = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="collected 1234 items", stderr="",
    )

    with patch("maestro.evolution.subprocess.run", return_value=finished) as run:
        result = e.collect_suite()

    assert result == (0, "collected 1234 items", "")
    argv = run.call_args.args[0]
    # The argv comes from the ``work_target`` path atoms, never a literal, and
    # carries the two flags that keep a collection probe bounded and side-effect
    # free.
    assert argv == [
        "pytest", e._config.work_target().gate_tests_path,
        "--collect-only", "-q", "-p", "no:cacheprovider",
    ]
    # No deadline is derived here — the flat ceiling applies.
    assert run.call_args.kwargs["timeout"] == e._config.selfcheck_timeout


def test_collect_suite_reports_a_nonzero_collection_red_as_a_triple(
    mem_fs: dict[str, str],
) -> None:
    """A collection *error* still finished, so it is a triple, not a non-answer —
    ``probe_collection`` is what turns it into ``UNCOLLECTABLE``."""
    e = _make_engine(mem_fs)
    errored = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="E   ImportError: maestro_sdk", stderr="boom",
    )

    with patch("maestro.evolution.subprocess.run", return_value=errored):
        result = e.collect_suite()

    assert result == (2, "E   ImportError: maestro_sdk", "boom")


def test_collect_suite_reports_a_killed_run_as_timeout(
    mem_fs: dict[str, str],
) -> None:
    """A collector that exceeded its budget is the bare ``TIMEOUT`` literal."""
    e = _make_engine(mem_fs)

    with patch(
        "maestro.evolution.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["pytest"], timeout=1),
    ):
        result = e.collect_suite()

    assert result == "TIMEOUT"


def test_collect_suite_reports_an_unstartable_runner_as_unavailable(
    mem_fs: dict[str, str],
) -> None:
    """A runner that never started is the bare ``UNAVAILABLE`` literal."""
    e = _make_engine(mem_fs)

    with patch("maestro.evolution.subprocess.run", side_effect=OSError("no pytest")):
        result = e.collect_suite()

    assert result == "UNAVAILABLE"


def test_collect_suite_output_feeds_probe_collection_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """The adapter's two shapes are exactly what the oracle's probe consumes."""
    from maestro import gate_environment as ge

    e = _make_engine(mem_fs)
    clean = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="collected 10 items", stderr="",
    )

    with patch("maestro.evolution.subprocess.run", return_value=clean):
        sound = ge.probe_collection(e.collect_suite)
    with patch("maestro.evolution.subprocess.run", side_effect=OSError("gone")):
        unanswered = ge.probe_collection(e.collect_suite)

    assert sound.kind == "SOUND"
    assert unanswered.kind == "UNCOLLECTABLE"


# ---------------------------------------------------------------------------
# Proposal 133 — a declining cached verdict refuses the gate run at the seam
# ---------------------------------------------------------------------------


def _seed_verdict(
    mem_fs: dict[str, str], engine: EvolutionEngine, **overrides: object,
) -> None:
    """Seed an ``UNCOLLECTABLE`` cached runnability row for *engine*."""
    row: dict[str, object] = {
        "kind": "UNCOLLECTABLE",
        "fingerprint": "fp",
        "identities": ["tests:COLLECTION_ERROR"],
        "detail": "collection COLLECTION_ERROR",
        "measured_at": "2026-09-21T00:00:00+00:00",
        "iteration_key": "iter-1",
    }
    row.update(overrides)
    mem_fs[str(engine._config.gate_environment_path)] = json.dumps(row)


def test_run_quality_gates_declined_spawns_nothing(mem_fs: dict[str, str]) -> None:
    """Flag on with a declining verdict, the runner issues zero subprocesses."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(mem_fs, e)

    with patch("maestro.evolution.subprocess.run") as mock_run:
        result = e.run_quality_gates()

    mock_run.assert_not_called()
    assert result.passed is False
    assert result.ruff_ok is False
    assert result.mypy_ok is False
    assert result.tests_ok is False
    assert result.raw_output == "collection COLLECTION_ERROR"
    # The coarse flag-off signature names every gate as failing; the fine
    # observation signature is the verdict's own identity, so the convergence
    # terminal compares the two with no translation.
    assert result.failure_signature() == frozenset({"ruff", "mypy", "tests"})
    assert result.observation is not None
    assert result.observation.signature() == frozenset({"tests:COLLECTION_ERROR"})


def test_run_quality_gates_declined_records_the_refusal(
    mem_fs: dict[str, str],
) -> None:
    """The refusal lands as one ``declined`` telemetry row stamped COLLECTION_ERROR."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(mem_fs, e)

    with patch("maestro.evolution.subprocess.run"):
        e.run_quality_gates()

    lines = mem_fs[str(e._config.gate_runs_path)].strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["admission"] == "declined"
    assert row["outcome"] == "COLLECTION_ERROR"


def test_run_quality_gates_declined_carries_every_verdict_identity(
    mem_fs: dict[str, str],
) -> None:
    """A verdict naming specific identities is reported under those identities."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(
        mem_fs, e, identities=["tests:a.py::test_one", "tests:b.py::test_two"],
    )

    with patch("maestro.evolution.subprocess.run"):
        result = e.run_quality_gates()

    assert result.observation is not None
    assert result.observation.signature() == frozenset(
        {"tests:a.py::test_one", "tests:b.py::test_two"},
    )
    assert result.observation.all_clean() is False


def test_run_quality_gates_declined_without_observation_flag(
    mem_fs: dict[str, str],
) -> None:
    """Flag composition is unchanged: observation off, the result carries none."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=False,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(mem_fs, e)

    with patch("maestro.evolution.subprocess.run") as mock_run:
        result = e.run_quality_gates()

    mock_run.assert_not_called()
    assert result.observation is None
    assert result.passed is False


def test_run_quality_gates_oracle_off_reads_no_verdict(
    mem_fs: dict[str, str],
) -> None:
    """Oracle off, a declining row on disk is never read and the gates still run."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=False,
        ),
    )
    _seed_verdict(mem_fs, e)

    with (
        # Patched at the *consumer*: gate_capacity binds ``read_verdict`` by
        # ``from ... import``, so patching the defining module would leave that
        # binding live and make the assertion below vacuous.
        patch("maestro.gate_capacity.read_verdict") as read,
        patch(
            "maestro.evolution.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="",
            ),
        ) as mock_run,
    ):
        result = e.run_quality_gates()

    read.assert_not_called()
    assert mock_run.call_count == 3
    assert result.passed is True


@pytest.mark.parametrize("kind", ["SOUND", "UNFIT"])
def test_run_quality_gates_admits_a_non_declining_verdict(
    mem_fs: dict[str, str], kind: str,
) -> None:
    """A sound — or merely ill-fitting — environment still runs every gate."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(mem_fs, e, kind=kind, identities=[])

    with patch(
        "maestro.evolution.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        ),
    ) as mock_run:
        result = e.run_quality_gates()

    assert mock_run.call_count == 3
    assert result.passed is True


def test_declined_result_is_flagged_and_never_banked_as_a_baseline(
    mem_fs: dict[str, str],
) -> None:
    """A declined run measured the environment, not the commit — bank nothing.

    Its COLLECTION_ERROR is a fact about a broken sibling install, and the
    baseline map is keyed by *sha* alone and served for the process's life, so
    banking it would survive the repair and let a later genuine collection red
    on the branch be excused as ``INHERITED``.
    """
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(mem_fs, e)
    e._vcs = MagicMock()
    e._vcs.get_head_hash.return_value = "sha1"
    e._vcs.has_changes.return_value = False

    with patch("maestro.evolution.subprocess.run") as mock_run:
        observation = e._observe_baseline("sha1")

    mock_run.assert_not_called()
    assert observation is None
    assert e._gate_baselines == {}


def test_declined_result_carries_the_declined_flag(mem_fs: dict[str, str]) -> None:
    """Only the synthesized result sets ``declined``; a real run leaves it False."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(mem_fs, e)

    with patch("maestro.evolution.subprocess.run"):
        declined = e.run_quality_gates()

    _seed_verdict(mem_fs, e, kind="SOUND", identities=[])
    with patch(
        "maestro.evolution.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        ),
    ):
        admitted = e.run_quality_gates()

    assert declined.declined is True
    assert admitted.declined is False


def test_declined_drops_identities_outside_the_tests_namespace(
    mem_fs: dict[str, str],
) -> None:
    """A non-``tests:`` identity is dropped, never re-labelled into ``tests:``.

    The synthesized observation is built with ``tool="tests"``, so keeping a
    ``mypy:`` identity would report a tests failure that never existed.
    """
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(
        mem_fs, e, identities=["tests:a.py::test_one", "mypy:maestro/x.py:3"],
    )

    with patch("maestro.evolution.subprocess.run"):
        result = e.run_quality_gates()

    assert result.observation is not None
    assert result.observation.signature() == frozenset({"tests:a.py::test_one"})


def test_declined_with_only_foreign_identities_falls_back_to_the_abort(
    mem_fs: dict[str, str],
) -> None:
    """Dropping every identity still leaves the honest whole-run collection abort."""
    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(mem_fs, e, identities=["ruff:maestro/x.py:1:1"])

    with patch("maestro.evolution.subprocess.run"):
        result = e.run_quality_gates()

    assert result.observation is not None
    assert result.observation.signature() == frozenset(
        {gate_environment.DECLINED_IDENTITY},
    )


def test_declined_failures_are_bounded_by_the_parser_cap(
    mem_fs: dict[str, str],
) -> None:
    """A verdict naming thousands of identities cannot escape the failure cap."""
    from maestro.gate_observation import MAX_FAILURES

    e = _observing_engine(
        Config(
            memory_dir=Path("/fake/evo_obs"),
            gate_observation_enabled=True,
            gate_environment_oracle_enabled=True,
        ),
    )
    _seed_verdict(
        mem_fs, e,
        identities=[f"tests:a.py::test_{n:04d}" for n in range(MAX_FAILURES + 25)],
    )

    with patch("maestro.evolution.subprocess.run"):
        result = e.run_quality_gates()

    assert result.observation is not None
    assert len(result.observation.gates[0].failures) == MAX_FAILURES


def test_plan_from_spec_caps_prompt_bound_prose_fields(
    mem_fs: dict[str, str],
) -> None:
    """Every prose field an oversized spec supplies is truncated (#3798)."""
    e = _make_engine(mem_fs)
    spec = {
        "title": "T" * (_PLAN_TITLE_MAX_CHARS + 300),
        "rationale": "R" * (_PLAN_RATIONALE_MAX_CHARS + 500),
        "proposed_changes": ["C" * (_PLAN_CHANGE_ITEM_MAX_CHARS + 400)],
        "expected_impact": "I" * (_PLAN_IMPACT_MAX_CHARS + 200),
    }

    plan = e._plan_from_spec(
        spec, default_rationale="fallback",
    )

    assert len(plan.title) == _PLAN_TITLE_MAX_CHARS
    assert len(plan.rationale) == _PLAN_RATIONALE_MAX_CHARS
    assert len(plan.expected_impact) == _PLAN_IMPACT_MAX_CHARS
    assert len(plan.proposed_changes) == 1
    assert len(plan.proposed_changes[0]) == _PLAN_CHANGE_ITEM_MAX_CHARS


def test_plan_from_spec_leaves_gate_input_lists_whole(
    mem_fs: dict[str, str],
) -> None:
    """No count cap, and no path truncation, on the lists gates read (#3798)."""
    e = _make_engine(mem_fs)
    long_path = "maestro/" + "f" * 400 + ".py"
    spec = {
        "proposed_changes": [f"change {i}" for i in range(50)],
        "files_affected": [long_path],
    }

    plan = e._plan_from_spec(
        spec, default_rationale="fallback",
    )

    assert len(plan.proposed_changes) == 50
    assert plan.files_affected == [long_path]


def test_plan_from_spec_keeps_tail_clause_for_cli_grounding(
    mem_fs: dict[str, str],
) -> None:
    """A novel CLI token in a tail clause survives normalisation (#3798).

    A ``proposed_changes`` count cap would drop this clause, leaving
    ``_cli_grounding_verdict`` looking at an all-already-shipped token set and
    wrongly disposing the plan ``obsolete``.
    """
    e = _make_engine(mem_fs)
    spec = {
        "proposed_changes": (
            [f"Modify maestro/loop step {i}" for i in range(40)]
            + ["Add a new maestro measure-novel-thing subcommand"]
        ),
    }

    plan = e._plan_from_spec(
        spec, default_rationale="fallback",
    )
    proposed_subs, _bound, _unbound = _extract_added_cli_surface(plan)

    assert "measure-novel-thing" in proposed_subs


def test_plan_from_spec_empty_changes_keeps_placeholder(
    mem_fs: dict[str, str],
) -> None:
    """Capping preserves the unparseable-plan placeholder fallback (#3798)."""
    e = _make_engine(mem_fs)

    plan = e._plan_from_spec(
        {"proposed_changes": []},
        default_rationale="fallback",
    )

    assert plan.proposed_changes == [UNPARSEABLE_PLAN_PLACEHOLDER]


@pytest.mark.asyncio
async def test_propose_plan_chain_slices_cap_prompt_bound_fields(
    mem_fs: dict[str, str],
) -> None:
    """The chain slice route inherits the same prompt-bound caps (#3798)."""
    e = _make_engine(mem_fs)
    long_path = "maestro/" + "f" * 400 + ".py"
    oversized = {
        "title": "T" * (_PLAN_TITLE_MAX_CHARS + 300),
        "category": "capability",
        "priority": "capability",
        "rationale": "R" * (_PLAN_RATIONALE_MAX_CHARS + 500),
        "proposed_changes": [
            "C" * (_PLAN_CHANGE_ITEM_MAX_CHARS + 400),
        ] + [f"change {i}" for i in range(30)],
        "acceptance_criteria": ["a works"],
        "files_affected": [long_path],
        "expected_impact": "I" * (_PLAN_IMPACT_MAX_CHARS + 200),
    }
    chain_json = json.dumps({
        "multi_pr": True,
        "proposal_title": "Big initiative",
        "proposal_goal": "Deliver X across two PRs",
        "slices": [oversized, {**oversized, "title": "Slice two"}],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=chain_json)
        result = await e.propose_plan_chain("some reflection")

    assert isinstance(result, tuple)
    _proposal, slices = result
    assert len(slices) == 2
    for s in slices:
        assert len(s.title) <= _PLAN_TITLE_MAX_CHARS
        assert len(s.rationale) == _PLAN_RATIONALE_MAX_CHARS
        assert len(s.expected_impact) == _PLAN_IMPACT_MAX_CHARS
        assert len(s.proposed_changes) == 31
        assert max(len(c) for c in s.proposed_changes) == _PLAN_CHANGE_ITEM_MAX_CHARS
        assert s.files_affected == [long_path]
