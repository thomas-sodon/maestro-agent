"""Red-root diagnosis — run the demanding gate as action-zero on a red lap.

Maestro's identity names a specific wound: on a CI-red remediation lap the first
thing it learns is the *safe* is-ancestor merge-truth (which obligates nothing),
and that truthful-but-cheap probe launders the skip of the *demanding* gate that
would read WHY CI is red. The loop already PUNISHES a lap that leaves a surfaced
red unaddressed
(:func:`maestro.outcome_grounding.bind_ci_failing_verdict_to_red_read` scrubs
resolution keywords), but nothing RUNS the gate and hands back the concrete
failure — that asymmetry is the anesthetic.

This module is the positive diagnostic. :func:`diagnose_red_root` invokes the
demanding gates in fast, deterministic order (ruff → mypy — pytest is excluded so
the read stays quick, and one mypy exit code already breaks the
base-red-and-mypy-avoidance wound at once) through an injected runner, returning
the first failing gate with its concrete first-error line, or a clean all-passed
sentinel when every gate exits ``0``. Wired at
:meth:`maestro.agent.MaestroAgent._preflight_pr_remediation` it makes the
demanding probe *action-zero* of a CI-red/pending own-PR lap, so the safe
merge-truth can no longer be the first thing known — and thus can no longer
launder the skip.

Pure given its injected runner: the runner is the ONLY side-effect seam, so the
diagnosis is deterministic under test with no subprocess of its own.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Callable, Sequence

from maestro.ci_diagnosis import (
    CheckRun,
    CIStatus,
    classify_ci_conclusions,
    failing_check_names,
)
from maestro.gate_environment import (
    GateRunnabilityVerdict,
    blames_missing_sibling,
    format_verdict,
)

# Re-exported from :mod:`maestro.ci_diagnosis` (the single CI-classification
# owner) so ``maestro.red_root.CheckRun`` / ``CIStatus`` / ``classify_ci_conclusions``
# / ``failing_check_names`` stay valid references for existing importers (e.g.
# ``maestro.agent``). Import direction is ``red_root → ci_diagnosis`` only.
__all__ = [
    "CIStatus",
    "CheckRun",
    "RedRootDiagnosis",
    "build_red_root_gates",
    "classify_ci_conclusions",
    "diagnose_red_root",
    "failing_check_names",
    "first_error_line",
    "render_remote_ci_section",
    "run_gate_subprocess",
]


def build_red_root_gates(
    source_path: str, tests_path: str, extra_paths: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Build the demanding gates (ruff → mypy) from the work-target gate atoms.

    Fast and deterministic — ruff then mypy; pytest is deliberately excluded so
    the red-root read stays quick. Each entry is ``(name, argv)`` where ``argv``
    is appended to ``[sys.executable, "-m", ...]`` by
    :func:`run_gate_subprocess`. *source_path* / *tests_path* come from the
    caller's :class:`~maestro.work_target.WorkTarget` (``maestro/`` + ``tests/``
    for self), so a ``[work]`` override retargets the diagnosis at another
    package. *extra_paths* are the lint-only atoms (``gate_lint_extra_paths``,
    ``scripts/`` for self) both gates also cover. These mirror the ruff/mypy
    gates in ``CLAUDE.md``'s Quality Gates block and CI's ``quality`` job; CI
    remains the authoritative enforcer.
    """
    return (
        ("ruff", ("ruff", "check", source_path, tests_path, *extra_paths)),
        ("mypy", ("mypy", source_path, *extra_paths)),
    )


#: The injected gate runner: given a gate ``name`` and its ``argv``, run it and
#: return ``(exit_code, first_error_line, full_output)``. This callable is the
#: sole side-effect seam of :func:`diagnose_red_root`, so a test injects a pure
#: stub and never touches a real subprocess. ``full_output`` carries the gate's
#: complete captured text (all lines, not just the first); the diagnosis does not
#: classify it — whether a red is an environment gap is the runnability oracle's
#: fact (:mod:`maestro.gate_environment`), threaded in as a verdict.
GateRunner = Callable[[str, tuple[str, ...]], tuple[int, str, str]]

@dataclasses.dataclass(frozen=True)
class RedRootDiagnosis:
    """The concrete root of a red lap — which gate failed, and its first error.

    :attr:`failing_gate` names the first gate that reported a non-zero exit
    (``None`` when every gate passed); :attr:`exit_code` and
    :attr:`first_error_line` carry that gate's exit code and first captured error
    line. :attr:`all_passed` is ``True`` iff no gate failed — the clean sentinel
    (``failing_gate is None``, ``exit_code == 0``, ``first_error_line == ""``).

    :attr:`ci_status` and :attr:`ci_failing_checks` carry the *remote* CI read
    when :func:`diagnose_red_root` is given check-run conclusions: the aggregate
    :data:`CIStatus` and the names of the concretely-failing checks. They stay
    ``None`` / ``()`` — their pre-remote-CI defaults — when no conclusions are
    supplied, so a ``None`` injection is byte-identical to the older diagnosis.
    """

    failing_gate: str | None
    exit_code: int
    first_error_line: str
    all_passed: bool
    ci_status: CIStatus | None = None
    ci_failing_checks: tuple[str, ...] = ()


def diagnose_red_root(
    run_gate: GateRunner,
    gates: Sequence[tuple[str, tuple[str, ...]]],
    check_runs: Sequence[CheckRun] | None = None,
    remote_ci_status: CIStatus | None = None,
    *,
    runnability: GateRunnabilityVerdict | None = None,
) -> RedRootDiagnosis:
    """Run the demanding gates in order, returning the first concrete failure.

    Invokes ``run_gate(name, argv)`` for each entry of *gates* (built by
    :func:`build_red_root_gates` from the caller's work-target atoms) in
    order (ruff → mypy) and returns a :class:`RedRootDiagnosis` naming the first
    gate that reports a non-zero exit, along with its exit code and first-error
    line. When every gate exits ``0`` it returns the clean all-passed sentinel
    (``failing_gate is None``, ``all_passed is True``). ``run_gate`` is the only
    side-effect seam, so this function is pure given its injected runner.

    *runnability* is the cached gate-runnability verdict (read, never probed,
    by the caller). When it blames a missing sibling install
    (:func:`~maestro.gate_environment.blames_missing_sibling`) the red gate's
    :attr:`~RedRootDiagnosis.first_error_line` leads with the oracle's own
    :func:`~maestro.gate_environment.format_verdict` line — which already ends in
    the repair — followed by the gate's concrete first error, so the recorded
    blocker names the environment gap without laundering a genuine defect. It
    applies to whichever gate is red — on a red ruff, which resolves no imports,
    the verdict is context beside the gate's own error rather than its cause —
    and never manufactures a red: an all-green
    run stays the clean sentinel. ``None`` (the default) leaves the line as the
    gate's first error.

    When *check_runs* is supplied (not ``None``) the remote-CI read is folded in:
    :attr:`~RedRootDiagnosis.ci_status` is the :func:`classify_ci_conclusions`
    verdict and :attr:`~RedRootDiagnosis.ci_failing_checks` names the concretely
    failing checks — so a FAILING lap targets a real defect rather than
    re-lapping a still-RUNNING CI. The ``None`` default leaves both fields at
    their pre-remote-CI defaults, keeping the diagnosis byte-identical.

    *remote_ci_status* is the caller's already-measured authoritative rollup
    verdict (the readiness :data:`~maestro.pr_facts.PRReadiness.ci_state`
    behind the chokepoint). It is consulted **only when *check_runs* is
    supplied** — the remote-CI fold lives inside the ``check_runs is not None``
    branch, so a *remote_ci_status* passed without *check_runs* leaves the
    remote-CI fields at their pre-remote-CI defaults (the diagnosis stays
    byte-identical) rather than the measured verdict taking silent effect; the
    sole caller always threads both together. When *check_runs* is present the
    verdict recovers a *name-less* ``FAILING`` that the failing-check *names*
    surface cannot see — an empty-name rollup item, or a timing skew between the
    two independent gh reads. A concretely-classified ``FAILING`` from real names
    always wins (preserving the mid-lap timing-skew case where a freshly-named
    failure appears while the aggregate still reads pending): the measured verdict
    is adopted only when the classified status is not already ``FAILING``.
    ``None`` (the default) never overrides the classifier, so every existing
    caller stays byte-identical.
    """
    ci_status: CIStatus | None = None
    ci_failing_checks: tuple[str, ...] = ()
    if check_runs is not None:
        ci_status = classify_ci_conclusions(check_runs)
        ci_failing_checks = failing_check_names(check_runs)
        if remote_ci_status is not None and ci_status != "FAILING":
            ci_status = remote_ci_status
    for name, argv in gates:
        exit_code, error_line, _full_output = run_gate(name, argv)
        if exit_code != 0:
            if blames_missing_sibling(runnability):
                error_line = (
                    f"{format_verdict(runnability)} — {name}'s first error: "
                    f"{error_line}"
                )
            return RedRootDiagnosis(
                failing_gate=name,
                exit_code=exit_code,
                first_error_line=error_line,
                all_passed=False,
                ci_status=ci_status,
                ci_failing_checks=ci_failing_checks,
            )
    return RedRootDiagnosis(
        failing_gate=None,
        exit_code=0,
        first_error_line="",
        all_passed=True,
        ci_status=ci_status,
        ci_failing_checks=ci_failing_checks,
    )


def first_error_line(stdout: str, stderr: str) -> str:
    """Return the first non-empty, stripped line across *stdout* then *stderr*.

    ruff and mypy both print their first diagnostic (``path:line: error: …``) as
    the first non-empty output line, so this is the concrete blocker to surface.
    Returns ``""`` when neither stream carries any non-empty line.
    """
    for stream in (stdout, stderr):
        for line in stream.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    return ""


def run_gate_subprocess(
    argv: tuple[str, ...],
    *,
    cwd: str,
    timeout: float,
) -> tuple[int, str, str]:
    """Real side-effecting gate runner: shell out and capture code + output.

    Runs ``[sys.executable, "-m", *argv]`` with ``check=False`` (so a non-zero
    exit is captured rather than raised) and ``capture_output=True`` so the
    concrete first-error line can be recovered via :func:`first_error_line`.
    Returns ``(exit_code, first_error_line, full_output)`` — the value shape a
    :data:`GateRunner` yields (the injected-runner adapter supplies the ``name``).
    ``full_output`` is the gate's complete captured text (stdout then stderr).

    A subprocess-launch or timeout failure (``OSError`` /
    :class:`subprocess.SubprocessError`, e.g. a timed-out gate) is translated into
    a non-zero verdict whose error line names the failure — the diagnostic
    surfaces the problem honestly rather than swallowing it, and never raises into
    the preflight it precedes.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-m", *argv],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        failure = f"gate runner failed to execute: {exc}"
        return 1, failure, failure
    return (
        completed.returncode,
        first_error_line(completed.stdout, completed.stderr),
        completed.stdout + completed.stderr,
    )


def render_remote_ci_section(diagnosis: RedRootDiagnosis) -> str:
    """Render the ``Remote CI`` artifact/journal lines for *diagnosis*.

    Returns ``""`` when :attr:`~RedRootDiagnosis.ci_status` is ``None`` (no
    check-run conclusions were supplied) — so a caller can unconditionally append
    the section and stay byte-identical to the pre-remote-CI output on the
    ``None`` path. Otherwise returns a ``- Remote CI: <STATUS>`` line, plus a
    ``- Failing checks: <names>`` line naming the concrete fix targets when the
    status is ``FAILING`` with named checks. Pure.
    """
    if diagnosis.ci_status is None:
        return ""
    lines = [f"- Remote CI: {diagnosis.ci_status}"]
    if diagnosis.ci_failing_checks:
        lines.append(
            "- Failing checks: " + ", ".join(diagnosis.ci_failing_checks),
        )
    return "\n".join(lines) + "\n"
