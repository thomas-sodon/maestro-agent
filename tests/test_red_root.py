"""Tests for red-root diagnosis — the demanding gate as action-zero on a red lap.

:mod:`maestro.red_root` is the positive complement of the punitive
``bind_ci_failing_verdict_to_red_read``: on a CI-red remediation lap it RUNS the
demanding gate (ruff → mypy) and hands back the concrete failing-gate blocker, so
the safe
is-ancestor merge-truth can no longer be the first thing known and thus can no
longer launder the skip. These tests pin the pure diagnosis, its subprocess
runner (with ``subprocess.run`` mocked — no real process), and the agent wiring
that makes it action-zero on a CI-failing/pending own PR (and skips it on GREEN),
per Testing Principles.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from maestro.ci_diagnosis import CIFailure
from maestro.config import Config
from maestro.consciousness import PendingPRRequest
from maestro.evolution_tend import CI_FAILURE_ON_HEAD_SUMMARY
from maestro.gate_check import build_gate_commands
from maestro.gate_environment import GateRunnabilityVerdict, format_verdict
from maestro.loop.pr_remediation import (
    RedRootDiagnosisError,
    _DequeueContext,
    _format_quality_gates_problem,
)
from maestro.pr_facts import CIState, PRReadiness
from maestro.red_root import (
    CheckRun,
    RedRootDiagnosis,
    build_red_root_gates,
    classify_ci_conclusions,
    diagnose_red_root,
    failing_check_names,
    first_error_line,
    render_remote_ci_section,
    run_gate_subprocess,
)

_ARTIFACT = "/fake/agent/artifacts/red-root-pr-8/red-root.md"

#: The demanding gates built from the default self atoms — the argument the
#: direct-call ``diagnose_red_root`` tests thread in place of the former
#: module-level ``RED_ROOT_GATES`` constant.
_GATES = build_red_root_gates("maestro/", "tests/", ("scripts/",))

# ── diagnose_red_root: pure, injected-runner-only ──────────────────────────


def _recording_runner(verdicts):  # type: ignore[no-untyped-def]
    """A pure stub :data:`~maestro.red_root.GateRunner` keyed by gate name.

    Records every ``(name, argv)`` it is called with so a test can assert the
    run order and short-circuiting, and returns the pre-programmed
    ``(exit_code, first_error_line, full_output)`` for that gate. A two-element
    ``(exit_code, first_error_line)`` verdict is normalised to a three-element one
    whose ``full_output`` mirrors the first-error line. It touches
    no subprocess — the injected runner is the diagnosis's only side-effect seam.
    """
    calls: list[tuple[str, tuple[str, ...]]] = []

    def _run(name: str, argv: tuple[str, ...]) -> tuple[int, str, str]:
        calls.append((name, argv))
        verdict = verdicts[name]
        if len(verdict) == 2:
            exit_code, error_line = verdict
            return exit_code, error_line, error_line
        return verdict

    return _run, calls


def test_diagnose_red_root_names_first_failing_gate() -> None:
    """ruff failing first → the diagnosis names ruff with its error line."""
    run, calls = _recording_runner(
        {
            "ruff": (1, "maestro/x.py:1:1: F401 unused import"),
            "mypy": (0, ""),
        },
    )
    diagnosis = diagnose_red_root(run, _GATES)
    assert diagnosis == RedRootDiagnosis(
        failing_gate="ruff",
        exit_code=1,
        first_error_line="maestro/x.py:1:1: F401 unused import",
        all_passed=False,
    )
    # Short-circuits on the first failure — mypy is never run.
    assert calls == [("ruff", _GATES[0][1])]


def test_diagnose_red_root_reaches_second_gate_when_first_passes() -> None:
    """ruff clean but mypy failing → the diagnosis names mypy, running both."""
    run, calls = _recording_runner(
        {
            "ruff": (0, ""),
            "mypy": (2, "maestro/y.py:12: error: Incompatible return value"),
        },
    )
    diagnosis = diagnose_red_root(run, _GATES)
    assert diagnosis.failing_gate == "mypy"
    assert diagnosis.exit_code == 2
    assert diagnosis.first_error_line == (
        "maestro/y.py:12: error: Incompatible return value"
    )
    assert diagnosis.all_passed is False
    assert [name for name, _ in calls] == ["ruff", "mypy"]


def test_diagnose_red_root_all_pass_sentinel() -> None:
    """Every gate exiting 0 yields the clean all-passed sentinel."""
    run, calls = _recording_runner({"ruff": (0, ""), "mypy": (0, "")})
    diagnosis = diagnose_red_root(run, _GATES)
    assert diagnosis == RedRootDiagnosis(
        failing_gate=None,
        exit_code=0,
        first_error_line="",
        all_passed=True,
    )
    # Both gates run because none failed.
    assert [name for name, _ in calls] == ["ruff", "mypy"]


# ── build_red_root_gates: owner-driven path substitution ───────────────────


def test_build_red_root_gates_default_atoms_are_ci_faithful() -> None:
    """The default self atoms are CI-faithful: ``scripts/`` joins ruff and mypy."""
    assert build_red_root_gates("maestro/", "tests/", ("scripts/",)) == (
        ("ruff", ("ruff", "check", "maestro/", "tests/", "scripts/")),
        ("mypy", ("mypy", "maestro/", "scripts/")),
    )


def test_build_red_root_gates_matches_the_gate_check_lint_paths() -> None:
    """Red-root diagnosis lints the same path set as ``maestro gate-check`` (#4128)."""
    wt = Config().work_target()
    red_root_gates = build_red_root_gates(
        wt.gate_source_path, wt.gate_tests_path, wt.gate_lint_extra_paths,
    )
    assert red_root_gates == build_gate_commands(wt)[:2]


def test_build_red_root_gates_honours_overridden_atoms() -> None:
    """Overridden atoms swap the source/tests paths in the ruff→mypy pair."""
    assert build_red_root_gates("widget/", "widget_tests/", ()) == (
        ("ruff", ("ruff", "check", "widget/", "widget_tests/")),
        ("mypy", ("mypy", "widget/")),
    )


# ── classify_ci_conclusions: pure remote-CI classifier ─────────────────────


@pytest.mark.parametrize(
    "failing_conclusion",
    [
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "error",
    ],
)
def test_classify_ci_conclusions_failing_on_any_failure(  # type: ignore[no-untyped-def]
    failing_conclusion,
) -> None:
    """Any failing conclusion makes the aggregate FAILING (a concrete target)."""
    runs = [
        CheckRun(name="tests", conclusion="success"),
        CheckRun(name="lint", conclusion=failing_conclusion),
        CheckRun(name="types", conclusion="in_progress"),
    ]
    assert classify_ci_conclusions(runs) == "FAILING"


@pytest.mark.parametrize(
    "failing_conclusion",
    [
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "error",
    ],
)
def test_classify_ci_conclusions_failing_even_when_name_missing(
    failing_conclusion: str,
) -> None:
    """A lone failing check with a missing name still classifies FAILING.

    Regression pin for overflow finding of-27708832: ``classify_ci_conclusions``
    reads only ``conclusion``, never ``name``, so an *unnamed* failing check-run
    — an empty-name rollup item, or a ``None`` a misbehaving host might yield — is
    still a concrete FAILING target rather than collapsing to a not-mine-this-turn
    RUNNING. The unnamed check is honestly absent from the named fix-target
    surface (:func:`failing_check_names` truthiness-guards the name), so the
    diagnosis reads 'FAILING, unnamed'.
    """
    for missing_name in ("", None):
        run = CheckRun(name=missing_name, conclusion=failing_conclusion)  # type: ignore[arg-type]
        assert classify_ci_conclusions([run]) == "FAILING"
        assert failing_check_names([run]) == ()


def test_classify_ci_conclusions_failing_on_startup_failure_and_error() -> None:
    """Terminal ``startup_failure`` / ``error`` read FAILING, not RUNNING.

    These two conclusions are in the authoritative failure vocabulary
    (:data:`maestro.ci_diagnosis.FAILING_CONCLUSIONS`) but were once absent from
    ``red_root``'s hand-mirrored copy, so an errored/startup-failed check
    silently fell through to RUNNING. Now the vocabulary has one owner; this pins
    them as concrete fix targets against the relocated classifier.
    """
    for conclusion in ("startup_failure", "error"):
        runs = [
            CheckRun(name="tests", conclusion="success"),
            CheckRun(name="deploy", conclusion=conclusion),
        ]
        assert classify_ci_conclusions(runs) == "FAILING"
        assert failing_check_names(runs) == ("deploy",)


def test_ci_classification_routes_through_single_owner() -> None:
    """Both ``vcs`` and ``red_root`` resolve CI classification through one owner.

    Replaces the former ``_FAILING_CONCLUSIONS`` mirrors ``vcs._FAILURE_STATES``
    test: those two hand-aligned private sets are gone, folded into
    :mod:`maestro.ci_diagnosis`. This pins the re-export identity (``red_root``'s
    classifiers ARE the owner's, so existing importers like ``maestro.agent``
    keep working) and that ``vcs`` binds the owner's predicates — the removed
    private sets no longer exist on either module. The owner's case-normalising
    predicate then resolves both the uppercase rollup idiom ``vcs`` fetches and
    the lower-cased check-run idiom ``red_root`` reads, so the two can never drift.
    """
    from maestro import ci_diagnosis, red_root, vcs

    # red_root re-exports the owner's symbols (identity, not a hand-mirrored copy).
    assert red_root.CheckRun is ci_diagnosis.CheckRun
    assert red_root.classify_ci_conclusions is ci_diagnosis.classify_ci_conclusions
    assert red_root.failing_check_names is ci_diagnosis.failing_check_names
    # vcs binds the owner's predicates; the removed private sets are gone.
    assert vcs.is_failing_conclusion is ci_diagnosis.is_failing_conclusion
    assert vcs.is_success_conclusion is ci_diagnosis.is_success_conclusion
    assert not hasattr(vcs, "_FAILURE_STATES")
    assert not hasattr(vcs, "_SUCCESS_STATES")
    assert not hasattr(red_root, "_FAILING_CONCLUSIONS")
    assert not hasattr(red_root, "_GREEN_CONCLUSIONS")
    # One owner resolves both idioms: uppercase (vcs fetch) and lower-cased
    # (red_root check-run) failure states classify identically.
    for state in ("FAILURE", "STARTUP_FAILURE", "ERROR"):
        assert ci_diagnosis.is_failing_conclusion(state) is True
        assert ci_diagnosis.is_failing_conclusion(state.lower()) is True


def test_classify_ci_conclusions_running_when_none_concluded() -> None:
    """All checks queued/in-progress and none concluded → RUNNING."""
    runs = [
        CheckRun(name="tests", conclusion="in_progress"),
        CheckRun(name="lint", conclusion="queued"),
        CheckRun(name="types", conclusion=""),
    ]
    assert classify_ci_conclusions(runs) == "RUNNING"


def test_classify_ci_conclusions_empty_is_running() -> None:
    """No check-runs at all → RUNNING (nothing has concluded)."""
    assert classify_ci_conclusions([]) == "RUNNING"


def test_classify_ci_conclusions_green_only_when_all_concluded_good() -> None:
    """GREEN only when every check concluded success/neutral/skipped."""
    runs = [
        CheckRun(name="tests", conclusion="success"),
        CheckRun(name="lint", conclusion="neutral"),
        CheckRun(name="types", conclusion="skipped"),
    ]
    assert classify_ci_conclusions(runs) == "GREEN"


def test_classify_ci_conclusions_not_green_when_one_still_running() -> None:
    """A single unconcluded check keeps the aggregate RUNNING, not GREEN."""
    runs = [
        CheckRun(name="tests", conclusion="success"),
        CheckRun(name="types", conclusion="in_progress"),
    ]
    assert classify_ci_conclusions(runs) == "RUNNING"


def test_failing_check_names_dedupes_and_preserves_order() -> None:
    """Only failing checks contribute their name, de-duplicated, in order."""
    runs = [
        CheckRun(name="tests", conclusion="failure"),
        CheckRun(name="lint", conclusion="success"),
        CheckRun(name="tests", conclusion="failure"),
        CheckRun(name="types", conclusion="timed_out"),
        CheckRun(name="", conclusion="cancelled"),
    ]
    assert failing_check_names(runs) == ("tests", "types")


# ── diagnose_red_root: remote-CI folding + None byte-identity ──────────────


def test_diagnose_red_root_folds_failing_ci_and_names() -> None:
    """Given failing check-runs, the diagnosis carries FAILING + the names."""
    run, _ = _recording_runner({"ruff": (0, ""), "mypy": (0, "")})
    diagnosis = diagnose_red_root(
        run,
        _GATES,
        check_runs=[
            CheckRun(name="CI / tests", conclusion="failure"),
            CheckRun(name="CI / lint", conclusion="success"),
        ],
    )
    assert diagnosis.ci_status == "FAILING"
    assert diagnosis.ci_failing_checks == ("CI / tests",)


def test_diagnose_red_root_none_check_runs_is_byte_identical() -> None:
    """The ``None`` default leaves both remote-CI fields at their defaults."""
    run, _ = _recording_runner({"ruff": (0, ""), "mypy": (0, "")})
    diagnosis = diagnose_red_root(run, _GATES)
    assert diagnosis == RedRootDiagnosis(
        failing_gate=None,
        exit_code=0,
        first_error_line="",
        all_passed=True,
    )
    assert diagnosis.ci_status is None
    assert diagnosis.ci_failing_checks == ()


def test_diagnose_red_root_remote_ci_recovers_nameless_failing() -> None:
    """Empty check-runs + measured FAILING → FAILING with no named checks.

    The name-less aggregate failure the failing-names surface cannot see (an
    empty-name rollup item, or a timing skew between the two gh reads) is
    recovered from the caller's authoritative rollup verdict, so a classify-only
    ``RUNNING`` (empty tuple) is upgraded to the measured ``FAILING`` rather than
    being silently downgraded to not-mine-this-turn.
    """
    run, _ = _recording_runner({"ruff": (0, ""), "mypy": (0, "")})
    diagnosis = diagnose_red_root(run, _GATES, check_runs=[], remote_ci_status="FAILING")
    assert diagnosis.ci_status == "FAILING"
    assert diagnosis.ci_failing_checks == ()


def test_diagnose_red_root_concrete_failing_beats_running_measure() -> None:
    """A concretely-classified FAILING wins over a measured RUNNING verdict.

    The mid-lap timing-skew guard: a freshly-named failure that appears while the
    aggregate rollup still reads pending keeps ``FAILING`` — the real names win.
    The measured verdict only *recovers* a name-less FAILING; it never downgrades
    a concretely-classified one.
    """
    run, _ = _recording_runner({"ruff": (0, ""), "mypy": (0, "")})
    diagnosis = diagnose_red_root(
        run,
        _GATES,
        check_runs=[CheckRun(name="CI / tests", conclusion="failure")],
        remote_ci_status="RUNNING",
    )
    assert diagnosis.ci_status == "FAILING"
    assert diagnosis.ci_failing_checks == ("CI / tests",)


def test_diagnose_red_root_remote_ci_none_is_classify_only() -> None:
    """``remote_ci_status=None`` never overrides the classifier (byte-identical).

    With a supplied ``check_runs`` the ``None`` default leaves ``ci_status`` at
    exactly the :func:`classify_ci_conclusions` verdict — here an empty tuple
    stays the classify-only ``RUNNING``, untouched by the new folding.
    """
    run, _ = _recording_runner({"ruff": (0, ""), "mypy": (0, "")})
    with_none = diagnose_red_root(run, _GATES, check_runs=[], remote_ci_status=None)
    without = diagnose_red_root(run, _GATES, check_runs=[])
    assert with_none.ci_status == "RUNNING"
    assert with_none == without


# ── runnability verdict: the oracle names a provisioning red ───────────────

_GENUINE_MYPY_LINE = (
    "maestro/foo.py:3: error: Incompatible return value type  [return-value]"
)


def _verdict(kind: str) -> GateRunnabilityVerdict:
    return GateRunnabilityVerdict(
        kind=kind,  # type: ignore[arg-type]
        fingerprint="f",
        detail="maestro_sdk is not installed; run pip install -e maestro_sdk/",
    )


def test_diagnose_red_root_leads_with_verdict_under_unprovisioned() -> None:
    """An UNPROVISIONED red mypy records the oracle line, keeping mypy's error."""
    verdict = _verdict("UNPROVISIONED")
    run, _ = _recording_runner(
        {"ruff": (0, ""), "mypy": (1, _GENUINE_MYPY_LINE)},
    )
    diagnosis = diagnose_red_root(run, _GATES, runnability=verdict)
    assert diagnosis.failing_gate == "mypy"
    assert diagnosis.first_error_line.startswith(format_verdict(verdict))
    assert diagnosis.first_error_line.endswith(
        f"mypy's first error: {_GENUINE_MYPY_LINE}",
    )


@pytest.mark.parametrize("kind", ["SOUND", "UNCOLLECTABLE", "UNFIT", None])
def test_diagnose_red_root_keeps_raw_line_when_verdict_does_not_blame(
    kind: str | None,
) -> None:
    """Only UNPROVISIONED rewrites the line; every other reading is byte-identical."""
    verdict = None if kind is None else _verdict(kind)
    run, _ = _recording_runner(
        {"ruff": (0, ""), "mypy": (1, _GENUINE_MYPY_LINE)},
    )
    diagnosis = diagnose_red_root(run, _GATES, runnability=verdict)
    assert diagnosis.first_error_line == _GENUINE_MYPY_LINE


def test_diagnose_red_root_verdict_applies_to_a_red_ruff() -> None:
    """No gate-name branch: a red ruff under UNPROVISIONED also leads with it."""
    verdict = _verdict("UNPROVISIONED")
    ruff_line = "maestro/x.py:1:1: F401 `os` imported but unused"
    run, _ = _recording_runner({"ruff": (1, ruff_line), "mypy": (0, "")})
    diagnosis = diagnose_red_root(run, _GATES, runnability=verdict)
    assert diagnosis.failing_gate == "ruff"
    assert diagnosis.first_error_line.startswith(format_verdict(verdict))
    assert diagnosis.first_error_line.endswith(f"ruff's first error: {ruff_line}")


def test_diagnose_red_root_verdict_never_manufactures_a_red() -> None:
    """All gates green under UNPROVISIONED stays the clean sentinel."""
    run, _ = _recording_runner({"ruff": (0, ""), "mypy": (0, "")})
    diagnosis = diagnose_red_root(
        run, _GATES, runnability=_verdict("UNPROVISIONED"),
    )
    assert diagnosis.all_passed is True
    assert diagnosis.first_error_line == ""


# ── render_remote_ci_section: pure artifact/journal fragment ───────────────


def test_render_remote_ci_section_names_failing_checks() -> None:
    """A FAILING status renders the status line plus the failing-check names."""
    section = render_remote_ci_section(
        RedRootDiagnosis(
            failing_gate="mypy",
            exit_code=2,
            first_error_line="boom",
            all_passed=False,
            ci_status="FAILING",
            ci_failing_checks=("CI / tests", "CI / lint"),
        ),
    )
    assert section == "- Remote CI: FAILING\n- Failing checks: CI / tests, CI / lint\n"


def test_render_remote_ci_section_running_has_no_names_line() -> None:
    """A RUNNING status renders the status line only (no failing-check line)."""
    section = render_remote_ci_section(
        RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="RUNNING",
        ),
    )
    assert section == "- Remote CI: RUNNING\n"


def test_render_remote_ci_section_empty_when_status_none() -> None:
    """No remote-CI read (ci_status None) → empty fragment (byte-identity)."""
    section = render_remote_ci_section(
        RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
        ),
    )
    assert section == ""


# ── first_error_line: pure ─────────────────────────────────────────────────


def test_first_error_line_prefers_first_nonempty_stdout_line() -> None:
    """The first non-empty, stripped stdout line wins over stderr."""
    line = first_error_line("\n  maestro/z.py:3: error: boom  \nmore", "stderr")
    assert line == "maestro/z.py:3: error: boom"


def test_first_error_line_falls_back_to_stderr() -> None:
    """An empty stdout falls through to the first non-empty stderr line."""
    assert first_error_line("   \n\n", "\nfatal: crash\n") == "fatal: crash"


def test_first_error_line_empty_when_no_output() -> None:
    """No non-empty line in either stream yields the empty string."""
    assert first_error_line("", "   \n\n") == ""


# ── run_gate_subprocess: subprocess.run mocked, never real ─────────────────


def test_run_gate_subprocess_captures_code_and_first_line() -> None:
    """The runner returns the subprocess exit code + captured first error line."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="maestro/a.py:1: error: nope\n", stderr="",
    )
    with patch(
        "maestro.red_root.subprocess.run", return_value=completed,
    ) as run_mock:
        code, line, output = run_gate_subprocess(
            ("mypy", "maestro/"), cwd="/ws", timeout=300,
        )
    assert code == 1
    assert line == "maestro/a.py:1: error: nope"
    # The full captured output (all lines) is surfaced, not just the first line.
    assert output == "maestro/a.py:1: error: nope\n"
    # Invoked as ``python -m mypy maestro/`` with capture and no raise.
    argv = run_mock.call_args.args[0]
    assert argv[1:] == ["-m", "mypy", "maestro/"]
    assert run_mock.call_args.kwargs["check"] is False
    assert run_mock.call_args.kwargs["cwd"] == "/ws"


def test_run_gate_subprocess_translates_launch_failure() -> None:
    """A subprocess/OS failure becomes a non-zero verdict naming the failure."""
    with patch(
        "maestro.red_root.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="mypy", timeout=1),
    ):
        code, line, output = run_gate_subprocess(
            ("mypy", "maestro/"), cwd="/ws", timeout=1,
        )
    assert code == 1
    assert "gate runner failed to execute" in line
    # The failure text is carried in both the first-error line and full output.
    assert "gate runner failed to execute" in output


# ── agent wiring: action-zero on CI-red/pending own PR ─────────────────────


def _readiness(ci_state: CIState) -> PRReadiness:
    """A minimal :class:`PRReadiness` carrying only the CI dimension under test."""
    return PRReadiness(
        pr_number=8,
        branch_landed=False,
        branch_behind=False,
        branch_conflicting=False,
        ci_state=ci_state,
        unresolved_threads=0,
        unaddressed_overflow=0,
        awaiting_approval=False,
        ready_and_waiting=ci_state == "GREEN",
        source="host_api",
        observed_at=0.0,
    )


def _entry(
    directive: str = "Remediate PR #8: post-merge failure",
) -> PendingPRRequest:
    return PendingPRRequest(directive=directive, pr_branch="maestro/evolve/p8")


def _wire_delegate(agent, verdict: bool) -> None:  # type: ignore[no-untyped-def]
    """Replace the controller preflight with a no-I/O async stub.

    The stub still runs the red-root diagnosis, as the real controller does
    inside its PR-checkout window (#4100), so these tests keep pinning the
    diagnosis semantics while the ordering is pinned against the real one.
    """

    async def _delegate(req: PendingPRRequest) -> bool:
        agent._diagnose_red_root_on_ci_red(req)
        return verdict

    agent._pr_remediation.preflight = _delegate  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_preflight_diagnoses_red_root_on_ci_failing(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A CI-FAILING own PR runs the demanding gate and injects the blocker."""
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(
            1,
            "maestro/x.py:1:1: F401 unused import",
            "maestro/x.py:1:1: F401 unused import",
        ),
    )
    # The oracle now runs unconditionally on a FAILING lap (proposal 102); stub
    # the log seam to "" so it contributes no concrete CI line, leaving the base
    # local-gate red-root blocker this test asserts.
    agent.evolution._host.read_run_log_failed = MagicMock(return_value="")

    result = await agent._preflight_pr_remediation(_entry())

    assert result is False
    agent._red_root_gate_runner.assert_called_once()
    # The concrete blocker lands in the red-root.md artifact.
    assert _ARTIFACT in mem_fs
    body = mem_fs[_ARTIFACT]
    assert "CI state: FAILING" in body
    assert "ruff (exit 1): maestro/x.py:1:1: F401 unused import" in body
    # And into the lap's operational event record.
    contents = [e.content for e in agent.journal.read_recent_events(5)]
    assert any(
        "Red-root on PR #8" in c and "ruff (exit 1)" in c for c in contents
    )


def _wire_mypy_red(agent) -> None:  # type: ignore[no-untyped-def]
    """A FAILING lap whose ruff passes and mypy is red with a genuine defect."""
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )

    def _runner(name: str, argv: tuple[str, ...]) -> tuple[int, str, str]:
        if name == "ruff":
            return 0, "", ""
        return 1, _GENUINE_MYPY_LINE, _GENUINE_MYPY_LINE

    agent._red_root_gate_runner = _runner  # type: ignore[method-assign]
    agent.evolution._host.get_pr_failing_check_names = MagicMock(return_value=[])
    agent.evolution._host.read_run_log_failed = MagicMock(return_value="")


@pytest.mark.asyncio
async def test_preflight_threads_cached_unprovisioned_verdict(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Flag on: the cached UNPROVISIONED verdict leads the recorded blocker."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _wire_mypy_red(agent)
    verdict = _verdict("UNPROVISIONED")
    with patch(
        "maestro.agent.gate_environment.read_verdict", return_value=verdict,
    ) as read_mock:
        result = await agent._preflight_pr_remediation(_entry())

    assert result is False
    read_mock.assert_called_once_with(agent.config.gate_environment_path)
    body = mem_fs[_ARTIFACT]
    assert f"mypy (exit 1): {format_verdict(verdict)}" in body
    assert f"mypy's first error: {_GENUINE_MYPY_LINE}" in body


@pytest.mark.asyncio
async def test_preflight_flag_off_never_reads_the_verdict(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Flag off: no verdict read, and mypy's raw first line is recorded."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = False
    _wire_mypy_red(agent)
    with patch("maestro.agent.gate_environment.read_verdict") as read_mock:
        result = await agent._preflight_pr_remediation(_entry())

    assert result is False
    read_mock.assert_not_called()
    assert f"mypy (exit 1): {_GENUINE_MYPY_LINE}" in mem_fs[_ARTIFACT]


@pytest.mark.asyncio
async def test_preflight_passes_code_host_check_runs_into_diagnose(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """The preflight threads the host's failing checks into ``diagnose_red_root``.

    Wiring assertion (acceptance criterion 5): the code host's failing check
    names are mapped to ``failure``-conclusion :class:`CheckRun`s and passed as
    ``check_runs``, and (criterion 4) the resulting Remote-CI section names the
    failing check in the ``red-root.md`` artifact.
    """
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent.evolution._host.get_pr_failing_check_names = MagicMock(
        return_value=["CI / pytest"],
    )
    agent.evolution._host.read_run_log_failed = MagicMock(return_value="")
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="FAILING",
            ci_failing_checks=("CI / pytest",),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    # ``diagnose_red_root`` received the host's checks as ``failure`` CheckRuns,
    # with the measured authoritative rollup verdict threaded alongside.
    assert diag.call_args.kwargs["check_runs"] == (
        CheckRun(name="CI / pytest", conclusion="failure"),
    )
    assert diag.call_args.kwargs["remote_ci_status"] == "FAILING"
    body = mem_fs[_ARTIFACT]
    assert "- Remote CI: FAILING" in body
    assert "- Failing checks: CI / pytest" in body


@pytest.mark.asyncio
async def test_preflight_records_failing_when_ci_rollup_failing_but_names_empty(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A FAILING rollup with no *named* failing check still records FAILING.

    A name-less aggregate failure — an empty-name rollup item, or a timing skew
    between the two independent gh reads — is recovered by threading the caller's
    already-measured authoritative rollup verdict into ``diagnose_red_root`` as
    ``remote_ci_status="FAILING"``, *not* by fabricating a stand-in CheckRun. So
    ``_observe_pr_check_runs`` returns ``()`` (no synthesis) yet the record stays
    ``FAILING`` and never contradicts itself ('CI state: FAILING' but 'Remote CI:
    RUNNING').
    """
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent.evolution._host.get_pr_failing_check_names = MagicMock(
        return_value=[],
    )
    agent.evolution._host.read_run_log_failed = MagicMock(return_value="")
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="FAILING",
            ci_failing_checks=(),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    # No fabricated CheckRun — the empty host result is threaded verbatim, and
    # the name-less FAILING is recovered from the authoritative rollup verdict.
    assert diag.call_args.kwargs["check_runs"] == ()
    assert diag.call_args.kwargs["remote_ci_status"] == "FAILING"
    body = mem_fs[_ARTIFACT]
    assert "- Remote CI: FAILING" in body
    # No name to surface — the finding is honestly 'FAILING, unnamed'.
    assert "- Failing checks:" not in body


@pytest.mark.asyncio
async def test_preflight_no_fallback_check_when_ci_pending_names_empty(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A PENDING rollup with no failing names threads RUNNING + empty check-runs.

    A still-PENDING CI with no named failing check is honestly
    not-mine-this-turn: ``_observe_pr_check_runs`` returns ``()`` and the caller
    threads the measured ``remote_ci_status="RUNNING"`` — no fabricated failure
    CheckRun, so the diagnosis reads RUNNING, not FAILING.
    """
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("PENDING"),
    )
    agent.evolution._host.get_pr_failing_check_names = MagicMock(
        return_value=[],
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="RUNNING",
            ci_failing_checks=(),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    assert diag.call_args.kwargs["check_runs"] == ()
    assert diag.call_args.kwargs["remote_ci_status"] == "RUNNING"


def test_record_diagnosis_without_ci_status_is_byte_identical(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A ci_status-less diagnosis records the pre-remote-CI artifact verbatim.

    Acceptance criterion 5's byte-identity clause: recording a diagnosis whose
    ``ci_status`` is ``None`` (the ``None``-injection path) appends no Remote-CI
    lines — the artifact is exactly the pre-change two-bullet body.
    """
    agent = make_agent()
    agent._record_red_root_diagnosis(
        8,
        "FAILING",
        RedRootDiagnosis(
            failing_gate="ruff",
            exit_code=1,
            first_error_line="maestro/x.py:1:1: F401 unused import",
            all_passed=False,
        ),
    )
    assert mem_fs[_ARTIFACT] == (
        "# Red-root diagnosis — PR #8\n\n"
        "- CI state: FAILING\n"
        "- Concrete blocker: ruff (exit 1): "
        "maestro/x.py:1:1: F401 unused import\n"
    )


def test_record_diagnosis_with_ci_failures_appends_section(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A non-empty ``ci_failures`` appends the ``## CI Failure`` artifact block.

    Proposal 102: the flag-on oracle threads the failing check's concrete error
    line into the durable ``red-root.md`` artifact after the Remote-CI section.
    """
    agent = make_agent()
    agent._record_red_root_diagnosis(
        8,
        "FAILING",
        RedRootDiagnosis(
            failing_gate="ruff",
            exit_code=1,
            first_error_line="maestro/x.py:1:1: F401 unused import",
            all_passed=False,
            ci_status="FAILING",
            ci_failing_checks=("quality",),
        ),
        ci_failures=(
            CIFailure(
                check_name="quality",
                first_error_lines=("FAILED tests/test_x.py::t",),
            ),
        ),
    )
    body = mem_fs[_ARTIFACT]
    assert body.endswith(
        "## CI Failure\n- quality:\n  - FAILED tests/test_x.py::t\n",
    )


@pytest.mark.asyncio
async def test_preflight_ci_failure_oracle_records_concrete_line(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Flag on + FAILING: the failing check's log line lands in artifact + journal."""
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    agent.evolution._host.read_run_log_failed = MagicMock(
        return_value=(
            "FAILED tests/test_x.py::test_y — AssertionError: nope\n"
        ),
    )
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="FAILING",
            ci_failing_checks=("quality",),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    # The log seam is read once for the parsed PR number.
    agent.evolution._host.read_run_log_failed.assert_called_once_with(8)
    body = mem_fs[_ARTIFACT]
    # Proposal 102 (this increment): the concrete CI failure IS the blocker —
    # the recomposed "Concrete blocker" line names the failing check and its
    # first error line, and the "look to pytest" anesthetic is gone.
    assert (
        "- Concrete blocker: CI check quality failed: "
        "FAILED tests/test_x.py::test_y — AssertionError: nope" in body
    )
    assert "look to pytest" not in body
    # The detailed per-check section is retained as supporting detail.
    assert "## CI Failure" in body
    assert "- quality:" in body
    assert "FAILED tests/test_x.py::test_y — AssertionError: nope" in body
    # The blocker already carries the line, so the journal realization states it
    # once (via the blocker) and drops the separate trailing sentence.
    content = agent.journal.record_event.call_args.kwargs["content"]
    assert (
        "The concrete blocker is CI check quality failed: "
        "FAILED tests/test_x.py::test_y — AssertionError: nope." in content
    )
    assert "The first CI failure line:" not in content


@pytest.mark.asyncio
async def test_preflight_ci_failure_oracle_failing_but_no_line(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Flag on + FAILING + unreadable log → honest 'no extractable line' blocker.

    Proposal 102: local gates pass and CI is concluded FAILING, but the failing
    run's log carries no recognisable ``FAILED`` / ``error:`` line, so the oracle
    extracts nothing. The recomposed blocker says so honestly (the
    ``render_ci_failure_section`` '(no concrete error line extracted)' voice)
    rather than pointing back to "look to pytest".
    """
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    # No recognisable error token, so ``extract_first_errors`` returns () and the
    # per-check ``CIFailure`` carries no line.
    agent.evolution._host.read_run_log_failed = MagicMock(
        return_value="build step ran; nothing recognisable here\n",
    )
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="FAILING",
            ci_failing_checks=("quality",),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    body = mem_fs[_ARTIFACT]
    assert (
        "- Concrete blocker: CI reports FAILING but its log carried no "
        "extractable error line" in body
    )
    assert "look to pytest" not in body
    # The detailed section still lists the check, noting it had no concrete line.
    assert "## CI Failure" in body
    assert "(no concrete error line extracted)" in body
    content = agent.journal.record_event.call_args.kwargs["content"]
    assert (
        "The concrete blocker is CI reports FAILING but its log carried no "
        "extractable error line." in content
    )
    assert "The first CI failure line:" not in content


@pytest.mark.asyncio
async def test_preflight_ci_failure_oracle_still_running(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Flag on + gates pass + CI still running → honest 'CI is still running'.

    Proposal 102: local gates pass and CI has not concluded a failure (a
    still-pending run classifies RUNNING). No failing log is read, and the
    recomposed blocker honestly says the red is not visible here yet instead of
    pointing "elsewhere".
    """
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("PENDING"),
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    agent.evolution._host.read_run_log_failed = MagicMock()
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="RUNNING",
            ci_failing_checks=(),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    # CI has not concluded a failure, so the log seam is never read.
    agent.evolution._host.read_run_log_failed.assert_not_called()
    body = mem_fs[_ARTIFACT]
    assert (
        "- Concrete blocker: ruff/mypy pass locally; CI is still running" in body
    )
    assert "look to pytest" not in body
    assert "## CI Failure" not in body
    content = agent.journal.record_event.call_args.kwargs["content"]
    assert (
        "The concrete blocker is ruff/mypy pass locally; CI is still running."
        in content
    )


@pytest.mark.asyncio
async def test_preflight_ci_oracle_surfaced_cache_true(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """FAILING + extractable line populates ``_ci_oracle_surfaced[pr]`` True.

    Proposal 102 follow-on: the preflight caches "a concrete failing-check line
    was surfaced for this PR this lap" so the record-time red-read bind can scope
    its downgrade without a second log fetch.
    """
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    agent.evolution._host.read_run_log_failed = MagicMock(
        return_value="FAILED tests/test_x.py::test_y — AssertionError: nope\n",
    )
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="FAILING",
            ci_failing_checks=("quality",),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    assert agent._ci_oracle_surfaced[8] is True


@pytest.mark.asyncio
async def test_preflight_ci_oracle_surfaced_cache_false_no_line(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """FAILING but no extractable line records ``_ci_oracle_surfaced[pr]`` False.

    Nothing concrete was surfaced, so the record-time bind must stand aside — the
    cache reads the honest 'no concrete red' fact.
    """
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    agent.evolution._host.read_run_log_failed = MagicMock(
        return_value="build step ran; nothing recognisable here\n",
    )
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="FAILING",
            ci_failing_checks=("quality",),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())

    assert agent._ci_oracle_surfaced[8] is False


@pytest.mark.asyncio
async def test_preflight_ci_oracle_surfaced_cache_starts_empty_and_populates(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """The surfaced-line cache starts empty, and the preflight populates it.

    A stale surfaced fact must never author a later lap's verdict, so ``run_loop``
    clears ``_ci_oracle_surfaced`` on a genuinely fresh iteration alongside the
    merge-truth / readiness memos — that reset is driven end-to-end in
    ``tests/test_agent.py::test_clean_iteration_end_clears_ephemeral_state``. Here
    we pin the two preflight facts that reset relies on: the cache is empty before
    the first preflight, and a surfaced concrete line populates it.
    """
    agent = make_agent()
    assert agent._ci_oracle_surfaced == {}
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, ""),
    )
    agent.evolution._host.read_run_log_failed = MagicMock(
        return_value="FAILED tests/test_x.py::test_y — AssertionError: nope\n",
    )
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]
    diag = MagicMock(
        return_value=RedRootDiagnosis(
            failing_gate=None,
            exit_code=0,
            first_error_line="",
            all_passed=True,
            ci_status="FAILING",
            ci_failing_checks=("quality",),
        ),
    )

    with patch("maestro.agent.diagnose_red_root", diag):
        await agent._preflight_pr_remediation(_entry())
    assert agent._ci_oracle_surfaced == {8: True}


@pytest.mark.asyncio
async def test_preflight_diagnoses_red_root_on_ci_pending(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A CI-PENDING own PR also runs the demanding gate (red or not-yet-green)."""
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("PENDING"),
    )
    with patch(
        "maestro.agent.diagnose_red_root",
        return_value=RedRootDiagnosis(
            failing_gate="mypy",
            exit_code=2,
            first_error_line="maestro/y.py:9: error: bad",
            all_passed=False,
        ),
    ) as diag:
        await agent._preflight_pr_remediation(_entry())
    diag.assert_called_once()
    body = mem_fs[_ARTIFACT]
    assert "CI state: PENDING" in body
    assert "mypy (exit 2): maestro/y.py:9: error: bad" in body


@pytest.mark.asyncio
async def test_preflight_skips_red_root_on_ci_green(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A GREEN own PR has no red root — the demanding gate is NOT run."""
    agent = make_agent()
    _wire_delegate(agent, verdict=True)
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("GREEN"),
    )
    with patch("maestro.agent.diagnose_red_root") as diag:
        result = await agent._preflight_pr_remediation(_entry())
    assert result is True
    diag.assert_not_called()
    assert _ARTIFACT not in mem_fs


@pytest.mark.asyncio
async def test_preflight_skips_red_root_without_pr_header(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A directive with no ``Remediate PR #<n>:`` header never diagnoses."""
    agent = make_agent()
    _wire_delegate(agent, verdict=False)
    agent.observe_pr_readiness = MagicMock()  # type: ignore[method-assign]
    with patch("maestro.agent.diagnose_red_root") as diag:
        await agent._preflight_pr_remediation(
            PendingPRRequest(directive="something else", pr_branch=None),
        )
    diag.assert_not_called()
    # No PR could be parsed, so readiness is never even observed.
    agent.observe_pr_readiness.assert_not_called()


def test_red_root_gate_runner_binds_workspace_and_timeout(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """The bound runner adapts ``run_gate_subprocess`` with cwd + timeout."""
    agent = make_agent()
    with patch(
        "maestro.agent.run_gate_subprocess", return_value=(0, "", ""),
    ) as run_mock:
        code, line, output = agent._red_root_gate_runner(
            "ruff", ("ruff", "check"),
        )
    assert (code, line, output) == (0, "", "")
    run_mock.assert_called_once_with(
        ("ruff", "check"),
        cwd=str(agent.config.workspace),
        timeout=agent.config.selfcheck_timeout,
    )


def _git_recorder(
    calls: list[tuple[str, ...]], *, checkout_fails: bool = False,
    local_missing: bool = False, local_sha: str = "abc",
    origin_sha: str | None = "abc", behind: bool = False,
    ahead: bool = False, ff_fails: bool = False, probe_merge_ok: bool = False,
):  # type: ignore[no-untyped-def]
    """A fake ``subprocess.run`` for the real preflight, logging each argv.

    ``local_missing`` fails only the plain PR checkout (the ``origin`` fallback
    succeeds); ``checkout_fails`` fails both. ``local_sha``/``origin_sha``
    model the origin-head guard (#4601); ``behind``/``ahead`` answer the
    two ``merge-base --is-ancestor`` reads, and a passing ``--ff-only``
    moves HEAD to ``origin_sha``.
    """
    head = [local_sha]

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(tuple(cmd))
        if cmd[:2] == ["git", "checkout"] and cmd[2] != "main":
            fallback = cmd[2] == "-b"
            if checkout_fails or (local_missing and not fallback):
                if kw.get("check"):
                    raise subprocess.CalledProcessError(1, cmd)
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            if origin_sha is None:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{origin_sha}\n", stderr="",
            )
        if cmd == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{head[0]}\n", stderr="",
            )
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            hit = behind if cmd[3] == "HEAD" else ahead
            return subprocess.CompletedProcess(cmd, 0 if hit else 1, "", "")
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            if ff_fails:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
            head[0] = origin_sha or ""
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        stdout = "main\n" if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"] else ""
        # A conflicting probe-merge ends the probe early with ``False``.
        rc = 1 if (
            cmd[:2] == ["git", "merge"] and "--abort" not in cmd
            and not probe_merge_ok
        ) else 0
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")

    return _run


@pytest.mark.asyncio
async def test_preflight_red_root_runs_after_pr_checkout(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """The real preflight runs ruff/mypy only once the PR head is checked out."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("PENDING"),
    )
    agent.evolution._host.get_pr_failing_check_names = MagicMock(return_value=[])
    calls: list[tuple[str, ...]] = []

    def _gate(argv, **_kw):  # type: ignore[no-untyped-def]
        calls.append(("GATE", *argv))
        return (0, "", "")

    with (
        patch("maestro.loop.pr_remediation.subprocess.run", _git_recorder(calls)),
        patch("maestro.agent.run_gate_subprocess", side_effect=_gate),
    ):
        result = await agent._preflight_pr_remediation(_entry())

    assert result is False
    checkout = calls.index(("git", "checkout", "maestro/evolve/p8"))
    first_gate = next(i for i, c in enumerate(calls) if c[0] == "GATE")
    assert first_gate > checkout
    merge = next(i for i, c in enumerate(calls) if c[:2] == ("git", "merge"))
    assert first_gate < merge


@pytest.mark.asyncio
async def test_preflight_red_root_runs_on_origin_fallback_checkout(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """No local PR branch → it is created off ``origin`` and still diagnosed."""
    agent = make_agent()
    agent._diagnose_red_root_on_ci_red = MagicMock()  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []
    with patch(
        "maestro.loop.pr_remediation.subprocess.run",
        _git_recorder(calls, local_missing=True),
    ):
        await agent._preflight_pr_remediation(_entry())

    assert (
        "git", "checkout", "-b", "maestro/evolve/p8", "origin/maestro/evolve/p8",
    ) in calls
    agent._diagnose_red_root_on_ci_red.assert_called_once()


@pytest.mark.asyncio
async def test_preflight_red_root_skipped_when_checkout_fails(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A failed PR checkout runs no gate and writes no red-root artifact."""
    agent = make_agent()
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._red_root_gate_runner = MagicMock()  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []
    with patch(
        "maestro.loop.pr_remediation.subprocess.run",
        _git_recorder(calls, checkout_fails=True),
    ):
        result = await agent._preflight_pr_remediation(_entry())

    assert result is False
    agent._red_root_gate_runner.assert_not_called()
    assert _ARTIFACT not in mem_fs


@pytest.mark.asyncio
async def test_preflight_red_root_failure_propagates(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A diagnosis crash is not degraded into the preflight's ``False``."""
    agent = make_agent()
    agent._diagnose_red_root_on_ci_red = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom"),
    )
    calls: list[tuple[str, ...]] = []
    with (
        patch("maestro.loop.pr_remediation.subprocess.run", _git_recorder(calls)),
        pytest.raises(RedRootDiagnosisError) as excinfo,
    ):
        await agent._preflight_pr_remediation(_entry())
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert ("git", "checkout", "main") in calls


@pytest.mark.asyncio
async def test_dequeue_pre_merge_ci_lap_runs_real_diagnosis(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """#4216: a pre-merge ``ci_failure_on_head`` lap, driven through the real
    dequeue preflight runner and the real ``_diagnose_red_root_on_ci_red``, reads
    the failing-check log, caches the oracle-surfaced fact, and writes
    ``red-root.md`` — without ever reaching the pure-QG preflight."""
    agent = make_agent()
    directive = (
        "Remediate PR #8: t\n\n## Problem\n"
        + _format_quality_gates_problem(8, f"{CI_FAILURE_ON_HEAD_SUMMARY}: quality")
    )
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=_readiness("FAILING"),
    )
    agent._preflight_pr_remediation = MagicMock()  # type: ignore[method-assign]
    agent._red_root_gate_runner = MagicMock(  # type: ignore[method-assign]
        return_value=(0, "", ""),
    )
    agent.evolution._host.get_pr_failing_check_names = MagicMock(
        return_value=["quality"],
    )
    agent.evolution._host.read_run_log_failed = MagicMock(
        return_value="FAILED tests/test_x.py::test_y — AssertionError: nope\n",
    )
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.porcelain_status.return_value = ""
    agent._vcs = vcs
    ctx = _DequeueContext(
        goal=directive, pr_branch="maestro/evolve/p8", source="external",
        entry=_entry(directive),
    )

    skipped = await agent._pr_remediation._run_preflight_dequeue(ctx)

    assert skipped is False
    agent.evolution._host.read_run_log_failed.assert_called_once_with(8)
    assert agent._ci_oracle_surfaced[8] is True
    assert _ARTIFACT in mem_fs
    agent._preflight_pr_remediation.assert_not_called()
    assert vcs.checkout.call_args_list[0].args[0] == "maestro/evolve/p8"
    assert vcs.checkout.call_args_list[-1].args[0] == "main"


_BRANCH = "maestro/evolve/p8"


@pytest.mark.asyncio
async def test_preflight_local_behind_origin_fast_forwards_before_gates(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A local branch trailing origin is fast-forwarded before any measurement."""
    agent = make_agent()
    agent.preflight_passes = MagicMock()
    calls: list[tuple[str, ...]] = []
    agent._diagnose_red_root_on_ci_red = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda _r: calls.append(("DIAGNOSE",)),
    )
    with (
        patch(
            "maestro.loop.pr_remediation.subprocess.run",
            _git_recorder(
                calls, local_sha="X", origin_sha="Y", behind=True,
                probe_merge_ok=True,
            ),
        ),
    ):
        result = await agent._preflight_pr_remediation(_entry())
    assert result is True
    gate_idx = [i for i, c in enumerate(calls) if c[0] in {"ruff", "mypy", "pytest"}]
    assert len(gate_idx) == 3
    ff = calls.index(("git", "merge", "--ff-only", f"origin/{_BRANCH}"))
    assert ff < gate_idx[0]
    agent.preflight_passes.record.assert_called_once_with(8, "Y")
    assert ff < calls.index(("DIAGNOSE",))
    probe = calls.index(("git", "merge", "--no-ff", "--no-edit", "origin/main"))
    assert ff < probe
    assert ("git", "reset", "--hard", "Y") in calls
    assert ("git", "reset", "--hard", "X") not in calls


@pytest.mark.asyncio
async def test_preflight_local_diverged_from_origin_returns_false(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Diverged local/origin history skips nothing and runs no gate."""
    agent = make_agent()
    agent._diagnose_red_root_on_ci_red = MagicMock()  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []
    with (
        patch(
            "maestro.loop.pr_remediation.subprocess.run",
            _git_recorder(calls, local_sha="X", origin_sha="Y"),
        ),
    ):
        result = await agent._preflight_pr_remediation(_entry())
    assert result is False
    assert ("git", "merge", "--no-ff", "--no-edit", "origin/main") not in calls
    assert [c for c in calls if c[0] in {"ruff", "mypy", "pytest"}] == []
    agent._diagnose_red_root_on_ci_red.assert_not_called()
    assert ("git", "checkout", "main") in calls


@pytest.mark.asyncio
async def test_preflight_unresolvable_origin_returns_false(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """No ``origin/<branch>`` means CI's tree is unproven, so no skip."""
    agent = make_agent()
    agent._diagnose_red_root_on_ci_red = MagicMock()  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []
    with (
        patch(
            "maestro.loop.pr_remediation.subprocess.run",
            _git_recorder(calls, origin_sha=None),
        ),
    ):
        result = await agent._preflight_pr_remediation(_entry())
    assert result is False
    assert [c for c in calls if c[0] in {"ruff", "mypy", "pytest"}] == []
    agent._diagnose_red_root_on_ci_red.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_local_ahead_of_origin_stays_admissible(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Locally-ahead commits are measured as-is, with no fast-forward."""
    agent = make_agent()
    agent.preflight_passes = MagicMock()
    agent._diagnose_red_root_on_ci_red = MagicMock()  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []
    with (
        patch(
            "maestro.loop.pr_remediation.subprocess.run",
            _git_recorder(
                calls, local_sha="X", origin_sha="Y", ahead=True,
                probe_merge_ok=True,
            ),
        ),
    ):
        result = await agent._preflight_pr_remediation(_entry())
    assert result is True
    gate_idx = [i for i, c in enumerate(calls) if c[0] in {"ruff", "mypy", "pytest"}]
    assert len(gate_idx) == 3
    assert ("git", "merge", "--ff-only", f"origin/{_BRANCH}") not in calls
    agent.preflight_passes.record.assert_called_once_with(8, "X")
    agent._diagnose_red_root_on_ci_red.assert_called_once()
    assert ("git", "reset", "--hard", "X") in calls


@pytest.mark.asyncio
async def test_preflight_ff_failure_returns_false(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A failed fast-forward falls through to full remediation, tree restored."""
    agent = make_agent()
    agent._diagnose_red_root_on_ci_red = MagicMock()  # type: ignore[method-assign]
    calls: list[tuple[str, ...]] = []
    with patch(
        "maestro.loop.pr_remediation.subprocess.run",
        _git_recorder(
            calls, local_sha="X", origin_sha="Y", behind=True, ff_fails=True,
        ),
    ):
        result = await agent._preflight_pr_remediation(_entry())
    assert result is False
    agent._diagnose_red_root_on_ci_red.assert_not_called()
    assert ("git", "checkout", "main") in calls
