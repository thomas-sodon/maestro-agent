"""Post-merge quality-gate routing and auto-fix inside tend
(``maestro/evolution_tend.py``); split out of ``tests/test_evolution_tend.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from maestro import evolution_quality, evolution_tend
from maestro.config import Config
from maestro.evolution import EvolutionEngine, QualityGateResult
from maestro.evolution_tend import TendPRsResult
from maestro.gate_observation import (
    _MAX_OUTPUT_CHARS,
    GateFailure,
    GateObservation,
    GateRunObservation,
)
from maestro.pr_remediation_state import Marker
from maestro.vcs import MergeResult, VCSError
from tests.support.merge_truth import merge_oracle
from tests.test_evolution_tend import (
    FAKE_DIR,
    _failing_gates,
    _ground_vcs,
    _make_engine,
    _plan,
    _pr_state,
)

# ---------------------------------------------------------------------------
# Post-merge quality-gate auto-fix
# ---------------------------------------------------------------------------


def _passing_gates() -> QualityGateResult:
    return QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
        raw_output="all green",
    )


@pytest.mark.asyncio
async def test_in_quality_fix_cooldown_helper(mem_fs: dict[str, str]) -> None:
    """``_in_quality_fix_cooldown`` honours quality_fix_cooldown_hours."""
    cfg = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR,
        quality_fix_cooldown_hours=24.0,
    )
    engine = _make_engine(mem_fs, config=cfg)
    plan = _plan()
    now = datetime.now().astimezone()

    # No prior attempt → not in cooldown.
    plan.last_quality_fix_attempt_at = None
    assert not evolution_tend._in_quality_fix_cooldown(engine, plan, now)

    # Within cooldown.
    plan.last_quality_fix_attempt_at = (now - timedelta(hours=1)).isoformat()
    assert evolution_tend._in_quality_fix_cooldown(engine, plan, now)

    # Past cooldown.
    plan.last_quality_fix_attempt_at = (now - timedelta(hours=25)).isoformat()
    assert not evolution_tend._in_quality_fix_cooldown(engine, plan, now)


def _mock_diff_proc() -> MagicMock:
    """Return a MagicMock standing in for ``subprocess.run`` of ``git diff``."""
    proc = MagicMock()
    proc.stdout = "diff stub"
    proc.returncode = 0
    return proc


@pytest.mark.asyncio
async def test_run_auto_fix_query_returns_changes_signal(
    mem_fs: dict[str, str],
) -> None:
    """``_run_auto_fix_query`` is the SDK-only piece, returns has_changes()."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.has_changes.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    plan = _plan()
    review = _failing_gates()
    with patch.object(evolution_quality, "query", new=AsyncMock()) as q, \
         patch("maestro.evolution.subprocess.run", return_value=_mock_diff_proc()):
        changed = await engine._run_auto_fix_query(
            plan, review, round_n=1, max_rounds=3,
        )
    assert changed is True
    q.assert_awaited_once()
    # Did NOT stage or commit — that's the caller's job.
    vcs.stage_all.assert_not_called()
    vcs.commit.assert_not_called()
    vcs.amend_commit.assert_not_called()


@pytest.mark.asyncio
async def test_run_auto_fix_query_forwards_disallowed_tools(
    mem_fs: dict[str, str],
) -> None:
    """The no-session auto-fix query carries the read-only external-MCP denial.

    Regression: the bare-``query`` else-branch attaches ``_mcp_servers`` (serena
    included while on) and must also forward ``external_mcp_disallowed_tools()``,
    or serena's ``execute_shell_command`` would be offered and could bypass the
    Bash-scoped action gate (proposal 084 read-only trust boundary).
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.has_changes.return_value = True
    cfg = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR, serena_mcp_enabled=True)
    engine = _make_engine(mem_fs, vcs=vcs, config=cfg)
    with patch.object(evolution_quality, "query", new=AsyncMock()) as q, \
         patch("maestro.evolution.subprocess.run", return_value=_mock_diff_proc()):
        await engine._run_auto_fix_query(
            _plan(), _failing_gates(), round_n=1, max_rounds=3,
        )
    opts = q.call_args.kwargs["options"]
    assert opts.disallowed_tools is not None
    assert "mcp__serena__execute_shell_command" in opts.disallowed_tools


@pytest.mark.asyncio
async def test_run_auto_fix_query_session_forwards_bypass_warning_throttle(
    mem_fs: dict[str, str],
) -> None:
    """The session auto-fix issues ``query_on_session`` with the bypass flag,
    so the gate-convergence grind skips the session-path warning-throttle
    sleep (proposal 110)."""
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    session = MagicMock()
    with patch(
        "maestro.evolution_quality.query_on_session", new=AsyncMock(),
    ) as qos, patch(
        "maestro.evolution.subprocess.run", return_value=_mock_diff_proc(),
    ):
        await engine._run_auto_fix_query(
            _plan(), _failing_gates(), session, round_n=1, max_rounds=3,
            bypass_warning_throttle=True,
        )
    assert qos.call_args.kwargs["bypass_warning_throttle"] is True


@pytest.mark.asyncio
async def test_run_auto_fix_query_session_defaults_no_bypass(
    mem_fs: dict[str, str],
) -> None:
    """Default (flag-off) session auto-fix leaves the throttle bypass off, so
    the legacy capped loop keeps the soft pacing byte-for-byte."""
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    session = MagicMock()
    with patch(
        "maestro.evolution_quality.query_on_session", new=AsyncMock(),
    ) as qos, patch(
        "maestro.evolution.subprocess.run", return_value=_mock_diff_proc(),
    ):
        await engine._run_auto_fix_query(
            _plan(), _failing_gates(), session, round_n=1, max_rounds=3,
        )
    assert qos.call_args.kwargs["bypass_warning_throttle"] is False


@pytest.mark.asyncio
async def test_run_auto_fix_query_fresh_context_forwards_bypass_warning_throttle(
    mem_fs: dict[str, str],
) -> None:
    """The no-session (fresh-context) auto-fix builds ``QueryOptions`` carrying
    the bypass flag, so the else-branch also skips the warning-throttle sleep."""
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    with patch.object(evolution_quality, "query", new=AsyncMock()) as q, \
         patch("maestro.evolution.subprocess.run", return_value=_mock_diff_proc()):
        await engine._run_auto_fix_query(
            _plan(), _failing_gates(), round_n=1, max_rounds=3,
            bypass_warning_throttle=True,
        )
    assert q.call_args.kwargs["options"].bypass_warning_throttle is True


@pytest.mark.asyncio
async def test_run_auto_fix_query_fresh_context_defaults_no_bypass(
    mem_fs: dict[str, str],
) -> None:
    """Default fresh-context auto-fix builds ``QueryOptions`` with the bypass
    off — byte-identical to the pre-change flag-off behaviour."""
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    with patch.object(evolution_quality, "query", new=AsyncMock()) as q, \
         patch("maestro.evolution.subprocess.run", return_value=_mock_diff_proc()):
        await engine._run_auto_fix_query(
            _plan(), _failing_gates(), round_n=1, max_rounds=3,
        )
    assert q.call_args.kwargs["options"].bypass_warning_throttle is False


@pytest.mark.asyncio
async def test_auto_fix_forwards_bypass_warning_throttle(
    mem_fs: dict[str, str],
) -> None:
    """``auto_fix`` forwards ``bypass_warning_throttle`` to
    ``_run_auto_fix_query`` (True passed through, default False)."""
    vcs = MagicMock()
    vcs.has_changes.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    run_mock = AsyncMock(return_value=True)
    with patch.object(engine, "_run_auto_fix_query", run_mock):
        await engine.auto_fix(
            _plan(), _failing_gates(), round_n=1, max_rounds=3,
            bypass_warning_throttle=True,
        )
    assert run_mock.await_args.kwargs["bypass_warning_throttle"] is True

    run_mock.reset_mock()
    with patch.object(engine, "_run_auto_fix_query", run_mock):
        await engine.auto_fix(
            _plan(), _failing_gates(), round_n=1, max_rounds=3,
        )
    assert run_mock.await_args.kwargs["bypass_warning_throttle"] is False


@pytest.mark.asyncio
async def test_local_auto_fix_still_amends(mem_fs: dict[str, str]) -> None:
    """The local-implementation ``auto_fix`` path keeps stage_all + amend_commit."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.has_changes.return_value = True
    engine = _make_engine(mem_fs, vcs=vcs)
    plan = _plan()
    review = _failing_gates()
    with patch.object(evolution_quality, "query", new=AsyncMock()), \
         patch("maestro.evolution.subprocess.run", return_value=_mock_diff_proc()):
        ok = await engine.auto_fix(plan, review, round_n=1, max_rounds=3)
    assert ok is True
    vcs.stage_all.assert_called_once()
    vcs.amend_commit.assert_called_once()


@pytest.mark.asyncio
async def test_local_auto_fix_no_changes_returns_false(
    mem_fs: dict[str, str],
) -> None:
    """auto_fix returns False when LLM made no changes; no commit shape applied."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.has_changes.return_value = False
    engine = _make_engine(mem_fs, vcs=vcs)
    with patch.object(evolution_quality, "query", new=AsyncMock()), \
         patch("maestro.evolution.subprocess.run", return_value=_mock_diff_proc()):
        ok = await engine.auto_fix(_plan(), _failing_gates(), round_n=1, max_rounds=3)
    assert ok is False
    vcs.stage_all.assert_not_called()
    vcs.amend_commit.assert_not_called()


class TestAutoFixQueryRoundContext:
    """Regression for #1680 — multi-round auto-fix prompt must surface
    that previous rounds already attempted a fix, otherwise the LLM
    re-issues the same edits on round 2/3."""

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_includes_round_context(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Round 2-of-3 prompt must call out the round budget and include the
        cumulative working-tree diff section so the LLM sees what previous
        rounds already changed."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        review = _failing_gates()

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        committed_diff = "diff --git a/x.py b/x.py\n+committed marker\n"
        cumulative_diff = "diff --git a/y.py b/y.py\n+cumulative marker\n"

        def _sub_side_effect(args: list[str], **_: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            if "origin/main...HEAD" in args:
                proc.stdout = committed_diff
            else:
                proc.stdout = cumulative_diff
            return proc

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", side_effect=_sub_side_effect,
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=2, max_rounds=3,
            )

        prompt = captured["prompt"]
        assert "Round 2 of 3" in prompt
        assert "## Working-tree edits since the failed commit" in prompt
        # The committed (base-branch ``origin/main...HEAD``) diff and the
        # cumulative HEAD diff land in distinct sections — the test must see
        # both, not just one.
        assert "committed marker" in prompt
        assert "cumulative marker" in prompt
        # Cumulative section comes after the committed-diff section so the LLM
        # sees "what was committed" → "what previous rounds changed" → "what's
        # still wrong" in narrative order.
        assert prompt.index("committed marker") < prompt.index("cumulative marker")
        # On a clean retrieval with non-empty stdout the preamble must
        # use the success-path wording — and must not leak the
        # timeout-fallback wording.
        assert "see the cumulative diff below" in prompt
        assert "could not be retrieved" not in prompt

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_round_one_omits_cumulative_section(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Round 1 has no prior attempt, so the cumulative diff section
        must not be emitted (empty section adds prompt noise) and the
        ``git diff HEAD`` subprocess call must not run at all."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        review = _failing_gates()

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", return_value=_mock_diff_proc(),
        ) as mock_run:
            await engine._run_auto_fix_query(
                plan, review, round_n=1, max_rounds=3,
            )

        prompt = captured["prompt"]
        assert "Round 1 of 3" in prompt
        assert "## Working-tree edits since the failed commit" not in prompt
        # Only the base-branch (committed) diff was fetched on round 1; no
        # cumulative HEAD diff call.
        called_args = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "diff", "origin/main...HEAD"] in called_args
        assert ["git", "diff", "HEAD"] not in called_args

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_cumulative_diff_timeout_falls_back(
        self, mem_fs: dict[str, str],
    ) -> None:
        """If ``git diff HEAD`` times out on round ≥ 2, the prompt still
        ships with a fallback marker instead of raising."""
        import subprocess as _sp

        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        review = _failing_gates()

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        def _sub_side_effect(args: list[str], **_: object) -> MagicMock:
            if "origin/main...HEAD" in args:
                proc = MagicMock()
                proc.returncode = 0
                proc.stdout = "committed diff"
                return proc
            raise _sp.TimeoutExpired("git", 30)

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", side_effect=_sub_side_effect,
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=2, max_rounds=3,
            )

        prompt = captured["prompt"]
        assert "(could not retrieve cumulative diff)" in prompt
        assert "## Working-tree edits since the failed commit" in prompt
        # The preamble must name the unavailability and steer the LLM
        # toward reading files, not promise a diff that isn't actually
        # below — that's the bug this regression locks out.
        assert "could not be retrieved" in prompt
        assert "read the relevant files before re-issuing edits" in prompt
        assert "see the cumulative diff below" not in prompt

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_round_two_empty_cumulative_diff_omits_section(
        self, mem_fs: dict[str, str],
    ) -> None:
        """When ``auto_fix()`` amends each round into HEAD, ``git diff HEAD``
        on round ≥ 2 returns empty stdout — the section must not be emitted
        and the preamble must not promise a cumulative diff that isn't
        there. Mirrors the agent.py/implement_plan path."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        review = _failing_gates()

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        def _sub_side_effect(args: list[str], **_: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            if "origin/main...HEAD" in args:
                proc.stdout = (
                    "committed diff with prior round edits already folded in"
                )
            else:
                # Working tree clean — prior round was amended into HEAD.
                proc.stdout = ""
            return proc

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", side_effect=_sub_side_effect,
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=2, max_rounds=3,
            )

        prompt = captured["prompt"]
        assert "Round 2 of 3" in prompt
        # No section header, no fallback marker, no broken promise about
        # a cumulative diff that doesn't exist.
        assert "## Working-tree edits since the failed commit" not in prompt
        assert "see the cumulative diff below" not in prompt
        # The adapted preamble points the LLM at the failing-commit diff
        # instead, which already includes prior rounds in this path.
        assert "folded into the failed commit" in prompt

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_long_context_widens_slices(
        self, mem_fs: dict[str, str],
    ) -> None:
        """With ``long_context=True`` the committed-diff and gate-output
        slices widen to their long-context caps (30_000 / 20_000). A
        synthesized review with an empty ``long_raw_output`` pins the fallback:
        the widened cap slices ``raw_output`` instead."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()

        # A committed diff and gate output that overflow the standard
        # 3000 / 2000 tail caps but fit inside the 30_000 / 20_000
        # long-context caps.  The head marker lives past the standard tail,
        # so its survival proves the widened slice was the one used.
        committed_diff = "COMMITTED_HEAD_MARKER\n" + "d" * 10_000
        review = QualityGateResult(
            passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
            raw_output="GATE_HEAD_MARKER\n" + "g" * 5_000,
        )

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        def _sub_side_effect(args: list[str], **_: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = committed_diff
            return proc

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", side_effect=_sub_side_effect,
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=1, max_rounds=3, long_context=True,
            )

        prompt = captured["prompt"]
        # Head markers survive only because the widened caps kept the whole
        # diff / gate output — the standard tail caps would have dropped them.
        assert "COMMITTED_HEAD_MARKER" in prompt
        assert "GATE_HEAD_MARKER" in prompt
        # Far more diff / gate content than the standard 3000 + 2000 tails.
        assert prompt.count("d") > 3000
        assert prompt.count("g") > 2000

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_long_context_widens_real_gate_output(
        self, mem_fs: dict[str, str],
    ) -> None:
        """#4167: a review produced by the real ``run_quality_gates`` (flag-off
        observation) carries enough gate output for the long-context cap to
        widen into — ``raw_output`` alone keeps only a 1000-char tail per gate,
        so a traceback 5K chars from the end was unreachable at any cap."""
        engine = _make_engine(mem_fs, stub_quality_gates=False)
        plan = _plan()
        pytest_stdout = "p" * 25_000 + "TRACEBACK_MARKER\n" + "t" * 5_000

        def _gate_run(cmd: list[str], **_: object) -> MagicMock:
            proc = MagicMock()
            is_pytest = "pytest" in cmd
            proc.returncode = 1 if is_pytest else 0
            proc.stdout = pytest_stdout if is_pytest else ""
            proc.stderr = ""
            return proc

        with patch("maestro.evolution.subprocess.run", side_effect=_gate_run):
            review = engine.run_quality_gates()

        assert review.observation is None
        assert review.raw_output == (
            f"Ruff: clean\n\nMypy: clean\n\nTests issues:\n{pytest_stdout[-1000:]}"
        )
        assert len(review.long_raw_output) <= _MAX_OUTPUT_CHARS
        assert review.long_raw_output.startswith("Ruff: clean\n\nMypy: clean")

        captured: list[str] = []

        async def _capture_query(prompt: str, **_: object) -> object:
            captured.append(prompt)
            return MagicMock()

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch("maestro.evolution.subprocess.run", side_effect=_gate_run):
            for long_context in (True, False):
                await engine._run_auto_fix_query(
                    plan, review, round_n=1, max_rounds=3,
                    long_context=long_context,
                )

        long_prompt, standard_prompt = captured
        assert "TRACEBACK_MARKER" in long_prompt
        assert "TRACEBACK_MARKER" not in standard_prompt
        assert "Tests issues:" in long_prompt

    def test_long_raw_output_keeps_every_red_gate_header(
        self, mem_fs: dict[str, str],
    ) -> None:
        """r1: with several red gates and oversized output, the long report
        shares the long-context cap so no header or earlier gate is dropped."""
        engine = _make_engine(mem_fs, stub_quality_gates=False)

        def _gate_run(cmd: list[str], **_: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 1
            proc.stdout = "RUFF_MARKER " + "r" * 30_000 if "ruff" in cmd else (
                "x" * 30_000
            )
            proc.stderr = ""
            return proc

        with patch("maestro.evolution.subprocess.run", side_effect=_gate_run):
            review = engine.run_quality_gates()

        long_out = review.long_raw_output
        assert len(long_out) <= _MAX_OUTPUT_CHARS
        assert long_out.startswith("Ruff issues:\n")
        assert "Mypy issues:\n" in long_out
        assert "Tests issues:\n" in long_out

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_default_caps_unchanged(
        self, mem_fs: dict[str, str],
    ) -> None:
        """With ``long_context`` defaulted off, the committed-diff and
        gate-output slices stay at the standard 3000 / 2000 tail caps —
        byte-identical to the pre-widening (flag-off) behaviour."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()

        committed_diff = "COMMITTED_HEAD_MARKER\n" + "d" * 10_000
        gate_output = "GATE_HEAD_MARKER\n" + "g" * 5_000
        review = QualityGateResult(
            passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
            raw_output=gate_output,
        )

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        def _sub_side_effect(args: list[str], **_: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = committed_diff
            return proc

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", side_effect=_sub_side_effect,
        ):
            # No ``long_context`` argument — the default (False) path.
            await engine._run_auto_fix_query(
                plan, review, round_n=1, max_rounds=3,
            )

        prompt = captured["prompt"]
        # The head markers sit past the standard tail caps, so they are
        # dropped — the 3000 / 2000 tails hold only the trailing content.
        assert "COMMITTED_HEAD_MARKER" not in prompt
        assert "GATE_HEAD_MARKER" not in prompt
        # Byte-identical to the standard caps: the embedded diff slice is
        # exactly the last 3000 chars, the gate slice exactly the last 2000.
        diff_region = prompt.split(
            "failed quality checks:\n```\n", 1,
        )[1].split("\n```", 1)[0]
        assert diff_region == committed_diff[-3000:]
        gate_region = prompt.split(
            "Quality gate output:\n", 1,
        )[1].split("\n\nFix ALL", 1)[0]
        assert gate_region == gate_output[-2000:]
        # Flag-off (no observation) the enumerated block never appears — the
        # diagnosis is byte-identical to the pre-124 raw-output tail.
        assert "## Quality gate failures" not in prompt
        assert "Attribution:" not in prompt

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_long_context_widens_cumulative_slice(
        self, mem_fs: dict[str, str],
    ) -> None:
        """On round ≥ 2 with ``long_context=True`` the cumulative
        ``git diff HEAD`` slice widens to the same 30_000 long-context cap as
        the committed diff, so a cumulative diff larger than the standard 3000
        tail survives intact. Closes the third widened slice the plan names —
        the round-1 tests cover only the committed diff and gate output, and a
        hard-coded 3000 on the cumulative slice would slip past them."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        review = _failing_gates()

        # A cumulative HEAD diff that overflows the standard 3000 tail but
        # fits the 30_000 long-context cap; its head marker lives past the
        # standard tail, so its survival proves the widened slice was used.
        committed_diff = "committed diff"
        cumulative_diff = "CUMULATIVE_HEAD_MARKER\n" + "c" * 10_000

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        def _sub_side_effect(args: list[str], **_: object) -> MagicMock:
            proc = MagicMock()
            proc.returncode = 0
            if "origin/main...HEAD" in args:
                proc.stdout = committed_diff
            else:
                proc.stdout = cumulative_diff
            return proc

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", side_effect=_sub_side_effect,
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=2, max_rounds=3, long_context=True,
            )

        prompt = captured["prompt"]
        # The cumulative section is present and its head marker survived —
        # only the widened 30_000 cap keeps the whole cumulative diff; the
        # standard 3000 tail would have dropped the marker.
        assert "## Working-tree edits since the failed commit" in prompt
        assert "CUMULATIVE_HEAD_MARKER" in prompt
        # Far more cumulative content than the standard 3000 tail could hold.
        assert prompt.count("c") > 5000

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_flag_on_renders_enumerated_failures(
        self, mem_fs: dict[str, str],
    ) -> None:
        """With an observation attached (flag-on), the diagnosis section is the
        enumerated per-gate failure block naming every failure identity plus the
        ``Attribution:`` verdict — not the front-truncated ``raw_output`` tail
        (proposal 124)."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        failures = tuple(
            GateFailure(
                tool="tests", kind="FAILED", location="tests/test_a.py",
                identifier=f"tests/test_a.py::test_{i}", message=f"boom {i}",
            )
            for i in range(4)
        )
        review = QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
            raw_output="RAW_TAIL_MARKER should not be the diagnosis",
            observation=GateRunObservation(
                gates=(
                    GateObservation(
                        tool="tests", kind="FAILED", failures=failures,
                        output="",
                    ),
                ),
            ),
        )

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", return_value=_mock_diff_proc(),
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=1, max_rounds=3,
                attribution="ATTRIBUTED",
            )

        prompt = captured["prompt"]
        assert "## Quality gate failures" in prompt
        for i in range(4):
            assert f"tests:tests/test_a.py::test_{i} — boom {i}" in prompt
        assert "Attribution: ATTRIBUTED" in prompt
        # The raw-output tail is replaced by the enumerated block, not appended.
        assert "RAW_TAIL_MARKER" not in prompt
        # ATTRIBUTED is not INHERITED, so the base-repair sentence is absent.
        assert "not this plan's to fix" not in prompt

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_inherited_adds_base_not_ours_sentence(
        self, mem_fs: dict[str, str],
    ) -> None:
        """An ``INHERITED`` verdict appends one sentence telling the fixer the
        failures belong to the branch's base and are not this plan's to fix, so
        the round spends itself on the diff rather than inherited breakage."""
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        review = QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=False, tests_ok=True,
            raw_output="ignored tail",
            observation=GateRunObservation(
                gates=(
                    GateObservation(
                        tool="mypy", kind="FAILED",
                        failures=(
                            GateFailure(
                                tool="mypy", kind="FAILED",
                                location="maestro/x.py",
                                identifier="maestro/x.py:1:name-defined",
                                message="Name 'foo' is not defined",
                            ),
                        ),
                        output="",
                    ),
                ),
            ),
        )

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", return_value=_mock_diff_proc(),
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=1, max_rounds=3,
                attribution="INHERITED",
            )

        prompt = captured["prompt"]
        assert "## Quality gate failures" in prompt
        assert "Attribution: INHERITED" in prompt
        assert "not this plan's to fix" in prompt
        # INHERITED is not ENVIRONMENT — the runnability sentence stays out.
        assert "could not reach a verdict at all" not in prompt

    @pytest.mark.asyncio
    async def test_run_auto_fix_query_environment_adds_no_diff_can_clear_sentence(
        self, mem_fs: dict[str, str],
    ) -> None:
        """An ``ENVIRONMENT`` verdict reaches the prompt explained, not bare.

        ``_attribute_review`` also runs per convergence round, so the attribution
        is forwarded into every auto-fix prompt. A bare ``Attribution:
        ENVIRONMENT`` line is a word the model reads as "try harder" when it means
        the opposite: the gate reached no verdict, so no diff can clear the red and
        rewriting working code to chase it is the failure mode.
        """
        vcs = MagicMock()
        vcs.has_changes.return_value = True
        engine = _make_engine(mem_fs, vcs=vcs)
        plan = _plan()
        review = QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
            raw_output="ignored tail",
            observation=GateRunObservation(
                gates=(
                    GateObservation(
                        tool="tests", kind="TIMEOUT", failures=(), output="",
                    ),
                ),
            ),
        )

        captured: dict[str, str] = {}

        async def _capture_query(prompt: str, **_: object) -> object:
            captured["prompt"] = prompt
            return MagicMock()

        with patch(
            "maestro.evolution_quality.query", new=AsyncMock(side_effect=_capture_query),
        ), patch(
            "maestro.evolution.subprocess.run", return_value=_mock_diff_proc(),
        ):
            await engine._run_auto_fix_query(
                plan, review, round_n=1, max_rounds=3,
                attribution="ENVIRONMENT",
            )

        prompt = captured["prompt"]
        assert "Attribution: ENVIRONMENT" in prompt
        assert "could not reach a verdict at all" in prompt
        assert "No code change can clear it" in prompt
        # ENVIRONMENT is not INHERITED — the base-repair sentence stays out.
        assert "not this plan's to fix" not in prompt


# ---------------------------------------------------------------------------
# Per-PR tend-session lifecycle (proposal: main-phase SDK calls)
# ---------------------------------------------------------------------------


def _merge_ok_engine(
    mem_fs: dict[str, str],
    *,
    head_sha: str = "sha-head-1",
) -> EvolutionEngine:
    """Build an engine whose VCS reports a successful clean merge."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.side_effect = lambda ref: (
        head_sha if ref.startswith("origin/maestro/") else "main-sha"
    )
    vcs.merge_from.return_value = MergeResult(
        success=True, conflicted_files=[],
    )
    return _make_engine(mem_fs, vcs=vcs)


@pytest.mark.asyncio
async def test_changes_requested_skips_when_no_staleness_signal(
    mem_fs: dict[str, str],
) -> None:
    """With no adapter staleness signal, CHANGES_REQUESTED still skips."""
    engine = _merge_ok_engine(mem_fs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            review_decision="CHANGES_REQUESTED",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.skipped_for_review == 1
    assert result.stale_review_resumed == 0
    engine._vcs.merge_from.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_changes_requested_resumes_when_head_advanced_by_sha(
    mem_fs: dict[str, str],
) -> None:
    """When the adapter exposes ``review_decision_head_sha`` and it does
    not match the current head SHA, tending resumes despite
    CHANGES_REQUESTED."""
    engine = _merge_ok_engine(mem_fs)
    _ground_vcs(engine._vcs, behind=1, merge_clean=True)
    engine._memory.save_evolution_plan(_plan())
    pr = _pr_state(
        review_decision="CHANGES_REQUESTED",
        merge_state_status="BEHIND",
    )
    pr.review_decision_head_sha = "sha-reviewed-0"  # type: ignore[attr-defined]
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": pr,
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.skipped_for_review == 0
    assert result.stale_review_resumed == 1
    assert result.merged_updated == 1


@pytest.mark.asyncio
async def test_changes_requested_skips_when_review_matches_head_sha(
    mem_fs: dict[str, str],
) -> None:
    """When the reviewed SHA equals the current head SHA the review is
    fresh — keep today's strict skip."""
    engine = _merge_ok_engine(mem_fs)
    engine._memory.save_evolution_plan(_plan())
    pr = _pr_state(review_decision="CHANGES_REQUESTED")
    # rev_parse returns ``sha-head-1`` for ``origin/<branch>`` per the
    # ``_streak_engine`` fixture.
    pr.review_decision_head_sha = "sha-head-1"  # type: ignore[attr-defined]
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": pr,
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.skipped_for_review == 1
    assert result.stale_review_resumed == 0


@pytest.mark.asyncio
async def test_changes_requested_resumes_when_head_committed_after_review(
    mem_fs: dict[str, str],
) -> None:
    """Timestamp fallback: when ``head_committed_at > review_decision_at``
    the review is stale → resume."""
    engine = _merge_ok_engine(mem_fs)
    _ground_vcs(engine._vcs, behind=1, merge_clean=True)
    engine._memory.save_evolution_plan(_plan())
    pr = _pr_state(
        review_decision="CHANGES_REQUESTED",
        merge_state_status="BEHIND",
    )
    pr.review_decision_at = "2026-05-01T00:00:00+00:00"  # type: ignore[attr-defined]
    pr.head_committed_at = "2026-05-02T00:00:00+00:00"  # type: ignore[attr-defined]
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": pr,
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.stale_review_resumed == 1
    assert result.skipped_for_review == 0
    assert result.merged_updated == 1


# ---------------------------------------------------------------------------
# Mechanical-only tend — quality-gate routing after a clean merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_merge_gate_failure_routes(mem_fs: dict[str, str]) -> None:
    """When merge succeeds but quality gates fail, the branch is reset
    and the gate summary is routed."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, stub_quality_gates=False)
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=False, tests_ok=False,
            raw_output="",
        ),
    )
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.quality_gates_routed == [
        (
            1, "t", "mypy, tests failed",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0
    # Proposal 137: the post-merge route stamps the same signature.
    stamped = engine._memory.get_plan("ep1")
    assert stamped is not None
    assert stamped.last_quality_fix_head_sha == "h"
    assert stamped.last_quality_fix_base_sha == "h"
    assert stamped.last_quality_fix_ci_conclusion == "success"
    # Post-merge gate failure rewinds only the failed merge to the recorded
    # pre-merge HEAD, not blunt-resetting to ``origin/<branch>``.
    vcs.reset_hard.assert_any_call("h")
    # Cooldown recorded so the next iteration does not re-route.
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_quality_fix_attempt_at is not None


@pytest.mark.asyncio
async def test_post_merge_gate_failure_records_completion_time(
    mem_fs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quality-fix cooldown record stamps the time the attempt actually
    completed, not the iteration-start ``now`` threaded through the loop
    (#1679).

    A single tend iteration captures one ``now`` at the top and reuses it for
    every open PR; the post-merge route then runs ``run_quality_gates()`` —
    real wall-clock time — *before* writing the timestamp. Recording the stale
    iteration-start ``now`` understates the cooldown, so a small configured
    ``quality_fix_cooldown_hours`` could read as already-elapsed next
    iteration. The record must reflect when the gate run resolved. A mocked
    clock advances inside the stubbed gate run to simulate that elapsed time.
    """
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    clock = [t0]

    class _ClockDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return clock[0]

    # Every ``datetime.now(UTC)`` in evolution_tend.py now reads the mutable clock:
    # the iteration-start capture sees ``t0``; a fresh call at the record site
    # (post-fix) sees the advanced value.
    monkeypatch.setattr("maestro.evolution_tend.datetime", _ClockDatetime)

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, stub_quality_gates=False)

    def _advance_and_fail() -> QualityGateResult:
        # The gate run consumes real wall-clock time; simulate 10 minutes.
        clock[0] = t0 + timedelta(minutes=10)
        return QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=False, tests_ok=False,
            raw_output="",
        )

    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        side_effect=_advance_and_fail,
    )
    plan = _plan()
    # ``None`` → not in cooldown, so the post-merge route reaches the record.
    plan.last_quality_fix_attempt_at = None
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Routing still fires, exactly like the sibling test.
    assert result.quality_gates_routed == [
        (
            1, "t", "mypy, tests failed",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    stored = engine._memory.get_plan("ep1")
    assert stored is not None
    # The record is the gate-completion time (t0 + 10min), NOT iteration-start
    # ``now`` (t0). Under the pre-fix code this equals ``t0.isoformat()``.
    assert stored.last_quality_fix_attempt_at == (
        t0 + timedelta(minutes=10)
    ).isoformat()


@pytest.mark.asyncio
async def test_post_merge_gate_failure_preserves_local_commits(
    mem_fs: dict[str, str],
) -> None:
    """When the merge is clean but gates fail with local remediation commits
    ahead of origin, the branch is rewound only to the recorded pre-merge
    HEAD — never blunt-reset to ``origin/<branch>`` — so unpushed local work
    survives while routing still fires."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # Local remediation commits ahead of origin → pre-merge reset is skipped.
    vcs.commits_ahead_behind.return_value = (2, 0)
    vcs.list_remote_branches.return_value = []
    # Recorded pre-merge HEAD is a sentinel distinct from origin/<branch>.
    vcs.rev_parse.return_value = "premerge"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, stub_quality_gates=False)
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=False, tests_ok=False,
            raw_output="",
        ),
    )
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The failed merge is rewound to the recorded pre-merge HEAD, preserving
    # local remediation commits.
    vcs.reset_hard.assert_any_call("premerge")
    # The branch is never blunt-reset to origin/<branch>.
    assert (
        call("origin/maestro/evolve/ep1") not in vcs.reset_hard.call_args_list
    )
    # Routing and cooldown stamping are unchanged.
    assert result.quality_gates_routed == [
        (
            1, "t", "mypy, tests failed",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_quality_fix_attempt_at is not None


@pytest.mark.asyncio
async def test_post_merge_gate_failure_indeterminate_head_fails_safe(
    mem_fs: dict[str, str],
) -> None:
    """When the pre-merge HEAD is indeterminate (``rev_parse`` → ``None``),
    the post-merge gate-failure path skips the reset entirely (fail-safe,
    leaving the clean merge in place) rather than risk discarding unpushed
    remediation commits via ``origin/<branch>`` — while routing still fires."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (2, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    # Indeterminate ref — rev_parse returns None.
    vcs.rev_parse.return_value = None
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, stub_quality_gates=False)
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=False, tests_ok=False,
            raw_output="",
        ),
    )
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # No reset of any kind — the fail-safe leaves the clean merge in place.
    vcs.reset_hard.assert_not_called()
    # Routing still fires so a remediation task addresses the gate failure.
    assert result.quality_gates_routed == [
        (
            1, "t", "mypy, tests failed",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    plan = engine._memory.get_plan("ep1")
    assert plan is not None
    assert plan.last_quality_fix_attempt_at is not None


def _inherited_observation() -> GateRunObservation:
    """A one-failure red mypy observation whose failure sits in ``maestro/x.py``.

    Same identity as :func:`_tend_baseline_result`'s failure, so a baseline built
    from it makes the post-merge red ``INHERITED`` when the diff does not touch
    that file, and ``ATTRIBUTED`` when it does.
    """
    failure = GateFailure(
        tool="mypy", kind="FAILED", location="maestro/x.py",
        identifier="maestro/x.py:1:name-defined", message="",
    )
    return GateRunObservation(
        gates=(
            GateObservation(
                tool="mypy", kind="FAILED", failures=(failure,), output="",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_post_merge_gate_failure_inherited_holds_merge(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 124: an INHERITED post-merge red holds the clean merge (no reset)
    while still routing a remediation whose summary names the base failures rather
    than the coarse two-word gate label."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (2, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "premerge"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(
        mem_fs, vcs=vcs, stub_quality_gates=False,
        config=Config(
            memory_dir=FAKE_DIR, workspace=FAKE_DIR,
            gate_observation_enabled=True,
        ),
    )
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=False, tests_ok=False,
            raw_output="", observation=_inherited_observation(),
        ),
    )
    # The base carried the identical failure identity, so the red is INHERITED.
    engine._observe_baseline = MagicMock(  # type: ignore[method-assign]
        return_value=_inherited_observation(),
    )
    plan = _plan()
    # The Tend post-merge arm is the one attribution caller that still passes a
    # plan's declared ``files_affected`` as the diff side (#3793 re-grounded the
    # convergence/landing readers only), so an empty list is what makes the
    # ATTRIBUTED arm unreachable here. Pin the VCS diff empty too, so the test
    # cannot silently change meaning if that arm is ever re-grounded.
    plan.files_affected = []
    vcs.changed_paths.return_value = ()
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The clean merge is held — no reset of any kind (mirrors the fail-safe arm).
    vcs.reset_hard.assert_not_called()
    # Routing still fires, but the summary names the inherited base failures.
    assert len(result.quality_gates_routed) == 1
    summary = result.quality_gates_routed[0][2]
    assert str(Marker.INHERITED_RED) in summary
    assert "mypy:maestro/x.py:1:name-defined" in summary
    assert "mypy, tests failed" not in summary
    stored = engine._memory.get_plan("ep1")
    assert stored is not None
    assert stored.last_quality_fix_attempt_at is not None


@pytest.mark.asyncio
async def test_post_merge_gate_failure_attributed_rewinds(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 124: an ATTRIBUTED post-merge red keeps today's rewind to the
    pre-merge head and today's coarse two-word gate summary."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (2, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "premerge"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(
        mem_fs, vcs=vcs, stub_quality_gates=False,
        config=Config(
            memory_dir=FAKE_DIR, workspace=FAKE_DIR,
            gate_observation_enabled=True,
        ),
    )
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=True, mypy_ok=False, tests_ok=False,
            raw_output="", observation=_inherited_observation(),
        ),
    )
    engine._observe_baseline = MagicMock(  # type: ignore[method-assign]
        return_value=_inherited_observation(),
    )
    plan = _plan()
    # The failing file IS in the diff → the red is the branch's own (ATTRIBUTED).
    plan.files_affected = ["maestro/x.py"]
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Today's rewind to the pre-merge head still fires.
    vcs.reset_hard.assert_any_call("premerge")
    # Today's coarse gate summary — not the base-failure rendering.
    assert result.quality_gates_routed == [
        (
            1, "t", "mypy, tests failed",
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]


@pytest.mark.asyncio
async def test_tend_reconciles_diverged_remote_branch_head(
    mem_fs: dict[str, str],
) -> None:
    """#2508 — when the PR branch's own remote head has diverged (local
    commits ahead AND a foreign remote commit behind), tend absorbs
    ``origin/<branch>`` into the local branch *before* the ``origin/main``
    merge so the trailing push is a true fast-forward and the resolution
    lands. The pre-merge ``reset_hard`` stays skipped (local work preserved)
    and force-push stays banned."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    # Diverged: 71 local commits ahead, 1 foreign remote commit behind.
    vcs.commits_ahead_behind.return_value = (71, 1)
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The diverged remote head is reconciled BEFORE origin/main, in order.
    merged_refs = [c.args[0] for c in vcs.merge_from.call_args_list]
    assert merged_refs == ["origin/maestro/evolve/ep1", "origin/main"]
    # The diverged branch is never blunt-reset to its remote (local work kept).
    assert (
        call("origin/maestro/evolve/ep1")
        not in vcs.reset_hard.call_args_list
    )
    # The push is a fast-forward now: issued exactly once, no push error.
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    assert result.errors == []
    assert result.merged_updated == 1
    assert result.conflicts_routed == []


@pytest.mark.asyncio
async def test_tend_routes_diverged_remote_branch_head_conflict(
    mem_fs: dict[str, str],
) -> None:
    """#2508 — when reconciling the diverged ``origin/<branch>`` head itself
    conflicts with local work, tend aborts the merge, routes the conflict for
    remediation, and never pushes a diverged head (``origin/main`` is never
    even reached)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (71, 1)  # diverged
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["hot.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Only the reconcile merge runs; the origin/main merge is never reached.
    vcs.merge_from.assert_called_once()
    assert vcs.merge_from.call_args.args[0] == "origin/maestro/evolve/ep1"
    vcs.abort_merge.assert_called_once()
    # The conflict is routed for remediation; nothing is pushed.
    assert result.conflicts_routed == [
        (
            1, "t", ["hot.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    vcs.push.assert_not_called()
    assert result.errors == []
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_tend_diverged_reconcile_merge_error_is_surfaced(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """#2508 — a hard ``VCSError`` while reconciling the diverged
    ``origin/<branch>`` head is isolated into ``result.errors`` (and surfaced
    at WARNING by the fail-loud sink), never pushed, never crashing the
    sweep — mirroring the ``origin/main`` merge-failure handling."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (71, 1)  # diverged
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.side_effect = VCSError("merge boom")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert len(result.errors) == 1
    assert (
        "reconcile of diverged origin/maestro/evolve/ep1 failed"
        in result.errors[0]
    )
    assert "merge boom" in result.errors[0]
    warnings = [
        rec.getMessage() for rec in caplog.records
        if rec.levelno == logging.WARNING
    ]
    assert any("reconcile of diverged" in msg for msg in warnings)
    vcs.push.assert_not_called()
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_tend_diverged_reconcile_then_main_conflict_pushes_resolution(
    mem_fs: dict[str, str],
) -> None:
    """#2508 — when the diverged ``origin/<branch>`` head reconciles cleanly
    but the subsequent ``origin/main`` merge re-conflicts, the now-reconciled
    head (strictly ahead of its own remote) is pushed *this* cycle, not the
    next: clearing ``behind`` after the reconcile lets the local-resolution
    push (gated on ``behind == 0``) fire. The conflict is still routed."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (71, 1)  # diverged
    vcs.list_remote_branches.return_value = []
    # Reconcile (origin/<branch>) clean, then origin/main re-conflicts.
    vcs.merge_from.side_effect = [
        MergeResult(success=True, conflicted_files=[]),
        MergeResult(success=False, conflicted_files=["hot.py"]),
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Reconcile ran first, then the origin/main merge that re-conflicted.
    merged_refs = [c.args[0] for c in vcs.merge_from.call_args_list]
    assert merged_refs == ["origin/maestro/evolve/ep1", "origin/main"]
    # The reconciled resolution is delivered this cycle (behind cleared to 0).
    vcs.push.assert_called_once_with("maestro/evolve/ep1")
    vcs.abort_merge.assert_called_once()
    # The bounded conflict is still routed for remediation.
    assert result.conflicts_routed == [
        (
            1, "t", ["hot.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0
    assert result.errors == []


@pytest.mark.asyncio
async def test_premerge_reset_skipped_when_ahead_count_falsely_zero(
    mem_fs: dict[str, str],
) -> None:
    """``commits_ahead_behind`` never raises — a transient failure (timeout,
    lock, garbled rev-list) collapses to ``(0, 0)`` exactly like a genuinely
    even branch. When that false zero hides a prior remediation task's local
    conflict resolution (local HEAD differs from ``origin/<branch>``), the
    pre-merge reset MUST be skipped so the resolution survives to the trailing
    ``push`` — never blunt-reset to ``origin/<branch>`` (#2457 ambiguity)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    # Ambiguous (0, 0): could be even, could be a failed read. The local branch
    # actually carries unpushed remediation commits — its HEAD differs from
    # origin/<branch>, which is the disambiguating signal.
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)

    def _rev_parse(ref: str) -> str:
        if ref == "origin/maestro/evolve/ep1":
            return "remote-head"
        return "local-head"

    vcs.rev_parse.side_effect = _rev_parse
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The local resolution is preserved (no blunt reset) and pushed.
    assert (
        call("origin/maestro/evolve/ep1") not in vcs.reset_hard.call_args_list
    )
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_premerge_reset_runs_when_branch_genuinely_even(
    mem_fs: dict[str, str],
) -> None:
    """A genuinely-even branch (local HEAD == ``origin/<branch>`` and a
    trustworthy ``(0, 0)``) still takes the pre-merge reset — the
    disambiguation must not regress the no-remediation clean-merge path."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.rev_parse.return_value = "h"  # local and origin resolve identically
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.reset_hard.assert_any_call("origin/maestro/evolve/ep1")
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_tend_indeterminate_ahead_reconciles_behind_remote(
    mem_fs: dict[str, str],
) -> None:
    """#2568 — an indeterminate ``(0, 0)`` divergence read whose SHA check
    disagrees (``ahead = None``) must not fall through as a no-op: the
    branch may be genuinely behind its remote, so tend absorbs
    ``origin/<branch>`` (mirroring the diverged-arm reconcile, #2508)
    before the ``origin/main`` merge so the trailing push is a true
    fast-forward instead of a per-lap non-fast-forward rejection."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)

    def _rev_parse(ref: str) -> str:
        if ref == "origin/maestro/evolve/ep1":
            return "remote-head"
        return "local-head"

    vcs.rev_parse.side_effect = _rev_parse
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # origin/<branch> is reconciled BEFORE origin/main, in order.
    merged_refs = [c.args[0] for c in vcs.merge_from.call_args_list]
    assert merged_refs == ["origin/maestro/evolve/ep1", "origin/main"]
    # The indeterminate branch is never blunt-reset to its remote.
    assert (
        call("origin/maestro/evolve/ep1") not in vcs.reset_hard.call_args_list
    )
    assert result.errors == []
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_tend_indeterminate_reconcile_vcs_error_is_surfaced(
    mem_fs: dict[str, str],
) -> None:
    """#2568 — a hard ``VCSError`` while reconciling ``origin/<branch>``
    from the indeterminate arm is isolated into ``result.errors``, never
    pushed, never crashing the sweep — mirroring the diverged-arm
    handling (#2508)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)

    def _rev_parse(ref: str) -> str:
        if ref == "origin/maestro/evolve/ep1":
            return "remote-head"
        return "local-head"

    vcs.rev_parse.side_effect = _rev_parse
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.side_effect = VCSError("merge boom")
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert len(result.errors) == 1
    assert (
        "reconcile of diverged origin/maestro/evolve/ep1 failed"
        in result.errors[0]
    )
    assert "merge boom" in result.errors[0]
    vcs.push.assert_not_called()
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_tend_indeterminate_reconcile_conflict_routes(
    mem_fs: dict[str, str],
) -> None:
    """#2568 — when the indeterminate-arm reconcile of ``origin/<branch>``
    conflicts with local work, tend aborts the merge, routes the conflict
    for remediation, and never pushes (``origin/main`` is never reached),
    mirroring the diverged-arm conflict path (#2508)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)

    def _rev_parse(ref: str) -> str:
        if ref == "origin/maestro/evolve/ep1":
            return "remote-head"
        return "local-head"

    vcs.rev_parse.side_effect = _rev_parse
    vcs.list_remote_branches.return_value = []
    vcs.merge_from.return_value = MergeResult(
        success=False, conflicted_files=["hot.py"],
    )
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Only the reconcile merge runs; the origin/main merge is never reached.
    vcs.merge_from.assert_called_once()
    assert vcs.merge_from.call_args.args[0] == "origin/maestro/evolve/ep1"
    vcs.abort_merge.assert_called_once()
    assert result.conflicts_routed == [
        (
            1, "t", ["hot.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    vcs.push.assert_not_called()
    assert result.errors == []
    assert result.merged_updated == 0


@pytest.mark.asyncio
async def test_tend_indeterminate_reconcile_then_main_conflict_never_pushes(
    mem_fs: dict[str, str],
) -> None:
    """#2568 — when the indeterminate-arm reconcile succeeds but the
    subsequent ``origin/main`` merge conflicts, the local-resolution push
    stays closed: its gate also requires ``ahead is not None``, and
    ``ahead`` stays ``None`` on this arm — an unresolved ref is never
    pushed. The conflict is still routed. Counterpart of the diverged-arm
    ``test_tend_diverged_reconcile_then_main_conflict_pushes_resolution``."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)

    def _rev_parse(ref: str) -> str:
        if ref == "origin/maestro/evolve/ep1":
            return "remote-head"
        return "local-head"

    vcs.rev_parse.side_effect = _rev_parse
    vcs.list_remote_branches.return_value = []
    # Reconcile (origin/<branch>) clean, then origin/main conflicts.
    vcs.merge_from.side_effect = [
        MergeResult(success=True, conflicted_files=[]),
        MergeResult(success=False, conflicted_files=["hot.py"]),
    ]
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    merged_refs = [c.args[0] for c in vcs.merge_from.call_args_list]
    assert merged_refs == ["origin/maestro/evolve/ep1", "origin/main"]
    vcs.abort_merge.assert_called_once()
    vcs.push.assert_not_called()
    assert result.conflicts_routed == [
        (
            1, "t", ["hot.py"],
            "ep1", "b", "https://github.com/x/y/pull/1",
            "maestro/evolve/ep1",
        ),
    ]
    assert result.merged_updated == 0
    assert result.errors == []


@pytest.mark.asyncio
async def test_post_merge_gate_passing_pushes_normally(
    mem_fs: dict[str, str],
) -> None:
    """When gates pass after a clean merge, the branch is pushed."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)  # stub_quality_gates=True default
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 1
    assert result.quality_gates_routed == []
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_quality_fix_cooldown_skips_gate_run(
    mem_fs: dict[str, str],
) -> None:
    """When ``last_quality_fix_attempt_at`` is within cooldown, the gate
    run is skipped and the merge is pushed without re-checking."""
    recent_iso = datetime.now(UTC).isoformat()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs, stub_quality_gates=False)
    engine.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=QualityGateResult(
            passed=False, ruff_ok=False, mypy_ok=False, tests_ok=False,
            raw_output="",
        ),
    )
    plan = _plan()
    plan.last_quality_fix_attempt_at = recent_iso
    engine._memory.save_evolution_plan(plan)
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Cooldown bypassed the gate run; merge proceeded to push.
    engine.run_quality_gates.assert_not_called()
    assert result.merged_updated == 1


def test_summarize_failed_gates_lists_failing_categories() -> None:
    summary = EvolutionEngine._summarize_failed_gates(QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=False,
        raw_output="",
    ))
    assert summary == "ruff, tests failed"


def test_summarize_failed_gates_handles_unexpected_all_ok() -> None:
    """Defensive: if every gate flag is True, summary still produces text."""
    summary = EvolutionEngine._summarize_failed_gates(QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=True,
        raw_output="",
    ))
    assert summary == "quality gates failed"


# ---------------------------------------------------------------------------
# Pre-merge reset_hard gated on commits_ahead_behind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_merge_reset_skipped_when_local_commits_ahead(
    mem_fs: dict[str, str],
) -> None:
    """When the branch has local commits ahead of origin, the pre-merge
    ``reset_hard`` is skipped so remediation commits survive the merge.

    Why: a remediation task that ran earlier in the loop committed to this
    branch but its commits are not yet on origin (run()'s push can fail).
    Resetting to ``origin/branch`` would discard those commits — Tend PR
    preserves and delivers them as fallback.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (2, 0)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The pre-merge reset_hard must NOT have been called for the branch.
    # (Post-merge gate-failure reset would call it, but gates pass here.)
    reset_calls = [c.args for c in vcs.reset_hard.call_args_list]
    assert ("origin/maestro/evolve/ep1",) not in reset_calls
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_pre_merge_reset_runs_when_at_parity(
    mem_fs: dict[str, str],
) -> None:
    """When ``commits_ahead_behind == (0, 0)`` the pre-merge ``reset_hard``
    runs to clean up any stale local state (existing semantics)."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.reset_hard.assert_any_call("origin/maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_pre_merge_reset_skipped_when_ahead_count_indeterminate(
    mem_fs: dict[str, str],
) -> None:
    """``commits_ahead_behind`` never raises (its contract is "best-effort,
    returns ``(0, 0)`` on failure"), so the indeterminate signal is a ``(0, 0)``
    reading that ``rev_parse`` cannot corroborate — here the local HEAD is
    unresolvable (``None``). The pre-merge ``reset_hard`` must fail SAFE and be
    skipped: a prior remediation task may have committed a conflict resolution
    on this branch that a reset would silently destroy.

    The downstream ``merge_from`` + trailing ``push`` still run, validating
    and delivering the preserved tree. Since #2568 the indeterminate arm
    additionally reconciles ``origin/<branch>`` before the ``origin/main``
    merge, so ``merge_from`` runs twice, reconcile first.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    # Local branch HEAD is unresolvable → indeterminate, cannot trust (0, 0).
    vcs.rev_parse.side_effect = lambda ref: (
        None if ref == "maestro/evolve/ep1" else "remote-head"
    )
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # The pre-merge reset_hard must NOT have been called for the branch.
    reset_calls = [c.args for c in vcs.reset_hard.call_args_list]
    assert ("origin/maestro/evolve/ep1",) not in reset_calls
    # Merge + push still proceeded on the preserved tree; the reconcile of
    # origin/<branch> precedes the origin/main merge (#2568).
    assert result.merged_updated == 1
    merged_refs = [c.args[0] for c in vcs.merge_from.call_args_list]
    assert merged_refs == ["origin/maestro/evolve/ep1", "origin/main"]
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


# ---------------------------------------------------------------------------
# CLEAN-with-local-commits pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_pr_with_local_commits_invokes_tend(
    mem_fs: dict[str, str],
) -> None:
    """A CLEAN PR with local commits ahead of origin must NOT short-circuit
    — ``_tend_single_pr`` runs so its trailing ``push`` delivers the
    remediation commits as fallback.
    """
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "abc"  # branch resolves locally
    vcs.commits_ahead_behind.return_value = (1, 0)
    vcs.merge_from.return_value = MergeResult(success=True, conflicted_files=[])
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="CLEAN", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 1
    vcs.push.assert_called_once_with("maestro/evolve/ep1")


@pytest.mark.asyncio
async def test_clean_pr_without_local_commits_short_circuits(
    mem_fs: dict[str, str],
) -> None:
    """A CLEAN PR with no local commits ahead retains the existing
    short-circuit: ``_tend_single_pr`` is not invoked."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "abc"
    vcs.commits_ahead_behind.return_value = (0, 0)
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="CLEAN", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 0
    vcs.merge_from.assert_not_called()
    vcs.push.assert_not_called()


@pytest.mark.asyncio
async def test_clean_pr_branch_not_resolved_locally_short_circuits(
    mem_fs: dict[str, str],
) -> None:
    """A CLEAN PR whose branch is not present locally falls through to
    the short-circuit — there are no local commits to push."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = None  # branch not present locally
    vcs.commits_ahead_behind.return_value = (0, 0)
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="CLEAN", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.merged_updated == 0
    vcs.merge_from.assert_not_called()
    vcs.push.assert_not_called()


def test_tend_prs_result_deferred_for_ci_default_zero() -> None:
    """The new counter defaults to zero like the other counters."""
    r = TendPRsResult()
    assert r.deferred_for_ci == 0


@pytest.mark.asyncio
async def test_ci_wait_get_conclusion_raises_treated_as_pending(
    mem_fs: dict[str, str],
) -> None:
    """A ``VCSError`` from ``get_pr_check_conclusion`` is treated as pending:
    the tend body defers without merging, mirroring the empty-string case."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.commits_ahead_behind.return_value = (0, 0)
    _ground_vcs(vcs, behind=1, merge_clean=True)
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "h"
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._host.get_pr_check_conclusion.side_effect = VCSError("gh down")  # type: ignore[attr-defined]
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="BEHIND", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    assert result.deferred_for_ci == 1
    vcs.merge_from.assert_not_called()


@pytest.mark.asyncio
async def test_branch_has_local_commits_ahead_returns_false_on_vcs_error(
    mem_fs: dict[str, str],
) -> None:
    """When ``commits_ahead_behind`` raises inside the CLEAN-pass-through
    probe, the short-circuit takes effect (treated as "no local commits")."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.list_remote_branches.return_value = []
    vcs.rev_parse.return_value = "abc"

    def _cab(a: str, b: str) -> tuple[int, int]:
        # The local-commits-ahead probe raises; the grounded staleness observer
        # (origin/<branch> vs origin/main) stays level, so the CLEAN PR is
        # current and the ``_branch_has_local_commits_ahead`` error path under
        # test is the only signal. (In production both git probes swallow
        # VCSError internally — this simulates that failure at the probe.)
        if a == "maestro/evolve/ep1" and b == "origin/maestro/evolve/ep1":
            raise VCSError("git down")
        return (0, 0)

    vcs.commits_ahead_behind.side_effect = _cab
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(
            merge_state_status="CLEAN", mergeable="MERGEABLE",
        ),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Short-circuit fired; no merge attempted.
    assert result.merged_updated == 0
    vcs.merge_from.assert_not_called()
