"""On-demand aggregate quality-gate verdict (``maestro gate-check``).

This is the owned, loseable verdict named in Maestro's capabilities:
running ruff, mypy, and pytest to a real exit code on its own change with
no human in the loop. The capability existed long before any behaviour
invoked it; this module makes that act a first-class, frictionless command
rather than a verdict perpetually routed around.

``run_gate_check`` shells out to each gate, captures the per-gate exit code,
and aggregates them so the aggregate is ``0`` if and only if every gate
passed. Each gate's stdout is routed to *stderr* (its stderr already inherits
the parent's), so every live diagnostic streams to the operator on stderr
while the command's own stdout stays reserved for the verdict — the human
breakdown or, under ``--json``, a single cleanly parseable JSON line. The
aggregate is the single number that means anything.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from maestro import gate_capacity, gate_environment
from maestro.gate_observation import GateNonAnswer
from maestro.work_target import WorkTarget

logger = logging.getLogger(__name__)

#: Seconds the fetch-free ``origin/main`` read may take. Mirrors the
#: ``git_quick_timeout`` default every other one-shot ``git rev-parse`` in the
#: tree runs under, so a wedged git cannot hang the command.
_HEAD_READ_TIMEOUT = 10

#: Exit code recorded for a sibling gate whose ``cwd`` does not exist. The
#: shell's conventional "command could not be executed" code, so a missing
#: sibling package reads as a red gate in the breakdown and the ``--json``
#: payload rather than aborting the run with a traceback.
MISSING_SIBLING_CWD_EXIT_CODE = 127


def build_gate_commands(
    work_target: WorkTarget,
    *,
    lease: gate_capacity.GateLease | None = None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Build the three quality-gate commands from the :class:`WorkTarget` owner.

    Each entry is ``(name, argv)`` where ``argv`` is appended to
    ``[sys.executable, "-m", ...]``. The source/tests path atoms come from
    *work_target* (``maestro/`` + ``tests/`` for self) rather than being
    hardcoded, so a ``[work]`` override retargets these gates at another
    package by editing config alone. The lint-only extra atoms
    (``gate_lint_extra_paths``, ``scripts/`` for self) join the ruff and mypy
    argv but never pytest's. These mirror the ``Quality Gates`` block in
    ``CLAUDE.md`` and CI's ``quality`` job (ruff + mypy + pytest); CI remains the
    authoritative enforcer. This reader's pytest runs without coverage.

    Under the gate execution lease (proposal 126) the pytest entry additionally
    carries *lease*'s granted ``pytest-xdist`` width via
    :func:`gate_capacity.worker_args`; the default ``None`` lease (the flag-off
    rollback and a one-worker floor grant) yields ``()`` so the tuple stays
    byte-identical to today.
    """
    source = work_target.gate_source_path
    tests = work_target.gate_tests_path
    extra = work_target.gate_lint_extra_paths
    return (
        ("ruff", ("ruff", "check", source, tests, *extra)),
        ("mypy", ("mypy", source, *extra)),
        ("pytest", ("pytest", tests, "-q", *gate_capacity.worker_args(lease))),
    )


def build_sibling_gate_commands() -> tuple[tuple[str, tuple[str, ...], str | None], ...]:
    """Build the six sibling-package gate commands CI's ``package-quality`` job runs.

    Each entry is ``(name, argv, cwd)``: ``argv`` is appended to
    ``[sys.executable, "-m", ...]`` exactly as :func:`build_gate_commands`'
    entries are, and *cwd* is the directory the gate runs from — ``None`` for
    the three ``maestro_review`` gates (repo root, as CI runs them) and
    ``"maestro_sdk"`` for the three CI runs under ``working-directory:
    maestro_sdk``. Names are package-qualified (``ruff:maestro_review``,
    ``mypy:maestro_sdk``, …) so the breakdown and the ``--json`` payload stay
    unambiguous once these share one ``exit_codes`` dict with the maestro gates.

    Unlike :func:`build_gate_commands` these paths are *not* sourced from the
    :class:`WorkTarget` atoms: the atoms owner names the package under gate
    (maestro's own source + tests), whereas this set is a fixed mirror of the
    two standalone sibling packages CI checks in a separate job. The two bare
    ``tests`` atoms are deliberately spelled without a trailing slash: ruff and
    pytest treat them identically to CI's ``tests/``, but the literal ``tests/``
    is one of the two atoms ``tests/test_gate_command_owner.py`` reads as the
    hardcoded-path signature, so spelling it that way here would trip that guard
    on a command that is not a maestro gate at all. The ``maestro_review/`` and
    ``tests/test_maestro_review`` atoms carry no such constraint — neither is in
    that guard's set — and are spelled exactly as CI spells them.

    ``tests/test_gate_check.py`` pins this set against the workflow file itself,
    so a ``package-quality`` edit cannot silently desync the mirror.
    """
    return (
        ("ruff:maestro_review", ("ruff", "check", "maestro_review/", "tests/test_maestro_review"), None),
        ("mypy:maestro_review", ("mypy", "maestro_review/"), None),
        ("pytest:maestro_review", ("pytest", "tests/test_maestro_review", "-q", "--no-cov"), None),
        ("ruff:maestro_sdk", ("ruff", "check", "src", "tests"), "maestro_sdk"),
        ("mypy:maestro_sdk", ("mypy", "src"), "maestro_sdk"),
        ("pytest:maestro_sdk", ("pytest", "tests", "-q"), "maestro_sdk"),
    )


@dataclasses.dataclass(frozen=True)
class GateCheckResult:
    """Per-gate exit codes plus the single aggregate verdict.

    ``exit_codes`` is keyed by gate name in run order; ``aggregate`` is the
    one number callers (and the CLI's process exit) act on.
    """

    exit_codes: dict[str, int]

    @property
    def aggregate(self) -> int:
        """Return ``0`` iff every gate exited ``0``; else the first nonzero code.

        Iterating ``exit_codes`` preserves the run order in which
        :func:`run_gate_check` inserts entries, so the returned code belongs
        to the earliest failing gate — a meaningful nonzero, not a flat ``1``.
        """
        for code in self.exit_codes.values():
            if code != 0:
                return code
        return 0

    @property
    def failing_gates(self) -> list[str]:
        """Names of the gates that exited nonzero, in run order."""
        return [name for name, code in self.exit_codes.items() if code != 0]


def format_gate_check_json(
    result: GateCheckResult,
    *,
    runnability: gate_environment.GateRunnabilityVerdict | None = None,
) -> str:
    """Serialize a :class:`GateCheckResult` to a stable machine-readable JSON string.

    Pure and side-effect-free: emits each gate's exit code under its own key,
    the ``aggregate`` verdict, and ``passed`` (``True`` iff ``aggregate == 0``).
    Keys are sorted for deterministic output so consumers can diff or parse the
    result without depending on dict insertion order.

    A *runnability* verdict — supplied only when ``[loop]
    gate_environment_oracle_enabled`` is on — adds exactly one further key,
    ``runnability``, carrying its kind. The default ``None`` emits today's
    payload byte-identically, so the flag-off command is unchanged and a
    consumer parsing the old shape keeps working.
    """
    payload: dict[str, int | bool | str] = dict(result.exit_codes)
    payload["aggregate"] = result.aggregate
    payload["passed"] = result.aggregate == 0
    if runnability is not None:
        payload["runnability"] = runnability.kind
    return json.dumps(payload, sort_keys=True)


def refresh_runnability_verdict(
    work_target: WorkTarget,
    *,
    verdict_path: Path,
    runs_path: Path,
    selfcheck: int,
    selfcheck_max: int,
    budget_lease_owned: bool,
    cgroup_reserve: bool,
) -> gate_environment.GateRunnabilityVerdict:
    """Probe the proposal-133 runnability verdict for the CLI and persist it.

    The agent-free counterpart to ``maestro.loop.plan_holds.gate_environment_verdict``:
    it assembles exactly the evidence
    :func:`~maestro.gate_environment.refresh_verdict` needs, from the owners that
    already hold it — the telemetry rows, planned width and derived deadline from
    :mod:`maestro.gate_capacity` (``in_flight=0``, because the question is what a
    run *would* be granted if it started now), the ``origin/main`` head from a
    fetch-free ``git rev-parse``, and a collector that runs ``pytest
    --collect-only`` with its argv built from *work_target*'s path atoms rather
    than a literal — but reaches no :class:`~maestro.agent.MaestroAgent`, no SDK
    and no code host, so an operator can ask the oracle from the command line.

    The ``iteration_key`` is prefixed (``"gate-check:"`` plus the measured
    timestamp) so it can never collide with the loop's bare integer keys: every
    invocation therefore probes afresh — the operator ran the command *because*
    they believe they repaired something — and writes the row the loop's own
    consumers read next, so a proven repair releases the lease seam's refusals
    without waiting for the loop's next probe.

    *budget_lease_owned* is ``[loop] gate_budget_lease_owned_enabled``. It is
    required rather than defaulted, so a caller cannot silently get flag-off
    semantics while the flag is up; it selects which budget the fit reading is
    judged against (proposal 134): on,
    the lease-derived deadline and ``selfcheck_max`` as the escalation cap; off,
    ``selfcheck`` with no cap, because that is the ceiling ``_run_gate_command``
    actually kills a run at when the flag is down — and no derivation is spent.
    The ``pytest --collect-only`` probe keeps its own *selfcheck* timeout either
    way: collection is not the budgeted suite run.

    *cgroup_reserve* is ``[loop] gate_lease_cgroup_reserve_enabled``, likewise
    required: the planned width comes from
    :func:`~maestro.gate_capacity.uncontended_width` under the same reserve
    setting the admission seam uses, so the fit reading judges the rows that
    real runs at that width actually record.
    """
    readings = gate_capacity.read_gate_runs(runs_path)
    workers = gate_capacity.uncontended_width(
        gate_capacity.measure_host_capacity(), cgroup_reserve=cgroup_reserve,
    )
    if budget_lease_owned:
        deadline = gate_capacity.derive_deadline(
            readings,
            workers=workers,
            selfcheck=selfcheck,
            selfcheck_max=selfcheck_max,
        )
        cap = selfcheck_max
    else:
        deadline = selfcheck
        cap = 0
    measured_at = datetime.now(UTC).isoformat()

    def collect() -> tuple[int, str, str] | GateNonAnswer:
        """Run ``pytest --collect-only`` and narrow it to the oracle's shapes.

        Mirrors ``EvolutionEngine.collect_suite``: a finished run becomes the
        ``(returncode, stdout, stderr)`` triple, the deadline killing it becomes
        the bare ``"TIMEOUT"`` literal, and a runner that cannot start at all
        becomes ``"UNAVAILABLE"``. ``-p no:cacheprovider`` keeps the probe from
        writing a ``.pytest_cache`` into the workspace and ``-q`` bounds the
        output.
        """
        try:
            completed = subprocess.run(
                [
                    sys.executable, "-m", "pytest", work_target.gate_tests_path,
                    "--collect-only", "-q", "-p", "no:cacheprovider",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=str(work_target.repo_workspace),
                timeout=selfcheck,
            )
        except subprocess.TimeoutExpired:
            return "TIMEOUT"
        except (OSError, ValueError):
            # The runner never started — no interpreter, an unusable ``cwd``, a
            # malformed argv. That is the oracle's ``UNAVAILABLE`` non-answer,
            # not a crash: the operator asked whether the gate can answer, and
            # "it could not even start" is that answer.
            return "UNAVAILABLE"
        return completed.returncode, completed.stdout, completed.stderr

    return gate_environment.refresh_verdict(
        verdict_path,
        iteration_key=f"gate-check:{measured_at}",
        collector=collect,
        readings=readings,
        workers=workers,
        deadline_seconds=deadline,
        cap_seconds=cap,
        head=_origin_main_head(work_target),
        measured_at=measured_at,
    )


def _origin_main_head(work_target: WorkTarget) -> str:
    """The ``origin/main`` commit the verdict is stamped at, or ``""``.

    Fetch-free by construction — it reads the remote-tracking ref this checkout
    already has, exactly as the loop-side assembly does. Every failure shape (a
    ref that has never been fetched, a non-repository ``cwd``, no ``git`` on
    ``PATH``) yields the empty string, which is the ``head``-absent fingerprint
    rather than a crash: the head only scopes an ``UNCOLLECTABLE`` re-probe, so
    not knowing it costs at most one extra probe.

    Bounded by :data:`_HEAD_READ_TIMEOUT`, matching the ``git_quick_timeout``
    every other one-shot ``git rev-parse`` in the tree runs under: a wedged git
    (an index lock another process holds, an unresponsive filesystem) must not
    hang ``maestro gate-check`` forever for a fact whose absence costs one
    extra probe. A timeout is therefore just another empty-string shape.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(work_target.repo_workspace),
            timeout=_HEAD_READ_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def run_gate_check(
    work_target: WorkTarget,
    *,
    lease_slot_dir: Path | None = None,
    runs_path: Path | None = None,
    cgroup_reserve: bool = False,
    admission_cap: bool = False,
    full: bool = False,
) -> GateCheckResult:
    """Run ruff, mypy, and pytest, returning each exit code plus the aggregate.

    The gate commands are built from *work_target* via
    :func:`build_gate_commands`, so the gates cover whichever package the
    ``[work]`` gate atoms name (``maestro/`` + ``tests/`` by default). Each
    gate runs as ``[sys.executable, "-m", *argv]`` with ``check=False`` so a
    nonzero exit is captured rather than raised. Each gate's stdout is
    redirected to the parent's stderr (its stderr already inherits the
    parent's), so all live diagnostics stream to the operator on stderr and
    the caller's stdout is left clean for the verdict — critical so that
    ``--json`` emits a single parseable JSON line with no gate output mixed in.
    The returned :class:`GateCheckResult` exposes the per-gate codes and the
    aggregate (``0`` iff all three passed).

    Under the gate execution lease (proposal 126) the whole three-gate loop runs
    inside one :func:`gate_capacity.lease_for_gate_run` slot keyed off
    *lease_slot_dir*, and the granted width is spliced into the pytest gate. The
    default ``None`` slot dir (the flag-off rollback) takes no slot, reads no host,
    and reproduces today's commands exactly. When *runs_path* is given and the
    lease is on, one telemetry row is recorded for the run, stamped with a coarse
    ``CLEAN``/``FAILED`` outcome derived from the aggregate exit codes.
    *cgroup_reserve* carries ``[loop] gate_lease_cgroup_reserve_enabled`` through
    to the seam's worker-grant plan; its default ``False`` reproduces today's
    grant exactly. *admission_cap* likewise carries ``[loop]
    gate_lease_admission_cap_enabled`` to the seam's slot budget and wait.

    With *full* the six sibling-package gates from
    :func:`build_sibling_gate_commands` run after the maestro triple, inside the
    same lease slot, and their exit codes join the same ``exit_codes`` dict — so
    ``aggregate``, ``failing_gates`` and :func:`format_gate_check_json` widen
    with no change, and a red sibling alone makes the aggregate nonzero. That
    makes a local green CI-faithful: CI's ``package-quality`` job checks exactly
    this set. The default ``full=False`` runs the same three commands as today,
    byte-identical. A sibling gate whose ``cwd`` does not exist (``--full`` run
    from outside the repo root) is recorded as
    :data:`MISSING_SIBLING_CWD_EXIT_CODE` rather than raising, so the command
    still returns the red verdict it promises.
    """
    exit_codes: dict[str, int] = {}
    with gate_capacity.lease_for_gate_run(
        lease_slot_dir, runs_path=runs_path, cgroup_reserve=cgroup_reserve,
        admission_cap=admission_cap,
        # This on-demand aggregate spends no lease-granted deadline, so its rows
        # are named apart from the loop's budgeted ``in_process`` suite runs that
        # ``gate_capacity.derive_deadline`` reads.
        source="gate_check",
    ) as run:
        for name, argv in build_gate_commands(work_target, lease=run.lease):
            completed = subprocess.run(
                [sys.executable, "-m", *argv],
                check=False,
                stdout=sys.stderr,
            )
            exit_codes[name] = completed.returncode
        if full:
            # The sibling suites are small and stay serial, spending none of
            # the lease's granted xdist width. ``cwd`` is resolved by
            # ``subprocess`` against *this* process's working directory, which
            # the maestro triple's relative path atoms already require to be the
            # repo root; from there ``maestro_sdk`` matches CI's
            # ``working-directory: maestro_sdk``.
            for name, argv, cwd in build_sibling_gate_commands():
                try:
                    sibling = subprocess.run(
                        [sys.executable, "-m", *argv],
                        check=False,
                        stdout=sys.stderr,
                        cwd=cwd,
                    )
                except OSError as exc:
                    # A missing/unusable ``cwd`` (``maestro_sdk`` absent, or
                    # gate-check invoked from outside the repo root) makes
                    # ``subprocess`` raise before the child ever starts. The
                    # maestro triple degrades to clean nonzero gate exits in
                    # that situation; this narrow catch keeps the sibling set
                    # doing the same, so the command always returns a verdict
                    # instead of an unhandled traceback. The gate is recorded
                    # red — never skipped.
                    logger.warning(
                        "gate %s could not start in cwd %r: %s", name, cwd, exc
                    )
                    exit_codes[name] = MISSING_SIBLING_CWD_EXIT_CODE
                    continue
                exit_codes[name] = sibling.returncode
        # Stamp the outcome before the lease context exits (the seam records on
        # exit): a coarse CLEAN iff every gate exited zero, else FAILED.
        run.outcome = "CLEAN" if all(c == 0 for c in exit_codes.values()) else "FAILED"
    return GateCheckResult(exit_codes=exit_codes)
