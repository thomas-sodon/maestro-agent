"""Regression: every pytest-xdist gate surface must pin the ``loadfile`` scheduler.

``scripts/quality-gates.sh``'s ``gate_maestro`` runs the ~11.7k-test core suite
under ``-n auto`` (pytest-xdist) so a foreground run fits under the agent's Bash
timeout. Plain ``-n auto`` uses xdist's default ``load`` scheduler, which
round-robins *individual* tests across workers rather than keeping a file's
tests on one worker. Two properties of this suite make that risky for a gate
that must pass at 100%:

* ``tests/test_structural_guards.py`` hoists whole-tree AST reads into
  module-scoped fixtures (``maestro_symbols`` / ``maestro_functions_by_basename``)
  "so they happen once" — but under ``load`` every worker that draws one of that
  module's tests rebuilds the whole-tree parse, multiplying the cost.
* ``pytest-timeout`` runs with ``timeout_method = "thread"`` (wall-clock, not
  CPU time) sized against the *logical* CPU count, so the already budget-tight
  structural-guard / fault-seeding cases can exceed their timeout under CPU
  contention plus rebuilt-fixture overhead, flaking the hard gate.

``--dist loadfile`` keeps a file's tests on one worker, so the module-scoped
fixtures are built once per worker and the shared cost is amortised. This is a
static-text pin (the unit suite forbids running the shell script as a real
subprocess); it catches a future edit silently dropping the scheduler flag and
reintroducing the flakiness class, mirroring the pins in
``tests/test_quality_gate_hook_changed_files.py``.

The same scheduler and serial-fallback contract now also governs the ``quality``
job's pytest step in ``.github/workflows/quality-gates.yml``: that job owns a
single-tenant ephemeral runner container, so it runs ``-n auto --dist loadfile`` and re-runs
serially only when a failed parallel run classifies as a resource artifact
(proposal 125; classified via ``gate-classify``, proposal 126). This module owns that CI-step pin
too, so the workflow and the shell runner cannot drift apart. Unlike the shell
runner and the hand-run docs, the workflow is deliberately **not** a
``tests/support/gate_sites.py`` entry — see the ``_WORKFLOW_PATH`` note below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.support.gate_sites import COMMAND_DOC_GATE_SITES, SHELL_GATE_RUNNER

# The shell runner and the three hand-run command docs, from the shared gate-site
# owner (proposal 126, step 2) so this scheduler pin and the worker-count guard
# scan one list. Each doc prescribes the core suite the agent runs by hand and so
# must carry the same ``--dist loadfile`` scheduler the script pins, or the
# manual-gate path reintroduces the flakiness this PR removes (PR #3385 review).
_SCRIPT_FILE = SHELL_GATE_RUNNER
_COMMAND_DOCS = COMMAND_DOC_GATE_SITES

# Proposal 126, step 12: gate_maestro now classifies a failed parallel run instead
# of blindly re-running the whole suite serially. A resource-artifact token earns
# exactly one bounded-width retry; an answered-red token is returned red with no
# second run. These two sets are the classified-retry contract, seeded per token
# by the behavioural pins below.
_RETRY_TOKENS = frozenset({"COVERAGE", "TIMEOUT", "UNAVAILABLE"})
_ANSWERED_RED_TOKENS = frozenset({"FAILED", "COLLECTION_ERROR"})

# The exact leased core-suite command; it appears twice in gate_maestro — the
# captured parallel run and the single classified retry — so the retry also runs
# at a granted (bounded) width rather than full width. The width atoms come from
# the ``{worker_args}`` placeholder the wrapper splices from the lease owner
# (``gate_capacity.worker_args``), so the ``-n``/``--dist loadfile`` pairing is
# enforced there, not duplicated as a literal at this gate site.
_LEASED_CORE_SUITE = (
    "maestro gate-lease -- $PY -m pytest tests/ -v {worker_args}"
)


def _retry_arm_tokens(content: str) -> frozenset[str]:
    """The classifier tokens gate_maestro's retry ``case`` arm re-runs on.

    The retry decision lives in a shell ``case "$kind"`` whose one upper-case
    ``TOK|TOK|...)`` arm lists exactly the retry-worthy tokens. The script's other
    ``case`` blocks use digit arms (``stage_name``) or lower-case scope names
    (``add_group``), so the single upper-case arm is unambiguous — the assertion
    below fails loudly if that ever stops being true. Uses ``[^\\S\\n]*`` (leading
    horizontal whitespace only) so a greedy ``\\s*`` cannot span blank lines.
    """
    arms = re.findall(r"(?m)^[^\S\n]*([A-Z]+(?:\|[A-Z]+)*)\)", content)
    assert len(arms) == 1, f"expected exactly one upper-case case arm, found {arms!r}"
    return frozenset(arms[0].split("|"))


# The CI pytest gate lives in the Quality Gates workflow. It is declared here
# **locally** and deliberately **not** sourced from tests/support/gate_sites.py:
# adding .github/workflows/ to that shared list would drag the workflow into
# tests/test_gate_lease_owner.py's worker-count scan, which forbids naming a
# worker count. But a single-tenant ephemeral runner container has no in-flight peers to
# arbitrate a lease against, so `-n auto` is exactly the correct spelling there
# (proposal 125). This module pins only the scheduler + serial fallback on the
# CI step; the lease guard is correctly blind to it.
_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"
)
_PYTEST_STEP_NAME = "Tests with coverage (pytest)"


def _ci_quality_pytest_run() -> str:
    """Return the ``quality`` job's pytest step ``run:`` text from the workflow.

    Vacuity guard: a renamed job or step, a missing/duplicated step, or an empty
    ``run:`` reds loudly here rather than letting the text assertions below pass
    against an empty string.
    """
    data = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert isinstance(jobs, dict), jobs
    quality = jobs.get("quality")
    assert isinstance(quality, dict), (
        "quality-gates.yml must declare a 'quality' job; the CI-step scheduler "
        "pin has nothing to assert against if it was renamed."
    )
    steps = quality.get("steps")
    assert isinstance(steps, list), steps
    matches = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == _PYTEST_STEP_NAME
    ]
    assert len(matches) == 1, (
        f"the 'quality' job must have exactly one '{_PYTEST_STEP_NAME}' step; "
        f"found {len(matches)}. A rename would let the loadfile/fallback "
        "assertions pass vacuously."
    )
    run_text = matches[0].get("run")
    assert isinstance(run_text, str) and "pytest" in run_text, (
        f"the '{_PYTEST_STEP_NAME}' step must carry a pytest 'run:' block; "
        f"got {run_text!r}."
    )
    return run_text


def test_gate_maestro_pins_loadfile_scheduler() -> None:
    """The flag-off branch keeps ``--dist loadfile``; the lease branch routes it.

    Without an explicit ``--dist`` mode, ``-n auto`` falls back to ``load``,
    scattering a file's tests across workers and rebuilding the module-scoped
    whole-tree AST fixtures per worker — the flakiness the flag prevents.
    Proposal 126 branches the parallel run on ``maestro gate-lease --enabled``:
    the lease branch splices its worker width — the ``-n``/``--dist loadfile``
    pairing included — from the wrapper's ``{worker_args}`` placeholder, so the
    scheduler pin lives at the lease owner (``gate_capacity.worker_args``, pinned
    by ``tests/test_gate_capacity.py``) rather than as a literal here; the
    ``else`` branch keeps today's ``-n auto --dist loadfile`` line byte-for-byte
    as the rollback pin.
    """
    content = _SCRIPT_FILE.read_text(encoding="utf-8")
    # Rollback pin: the flag-off branch keeps today's literal verbatim.
    assert "-n auto --dist loadfile" in content, (
        "scripts/quality-gates.sh gate_maestro's flag-off branch must keep the "
        "'-n auto --dist loadfile' rollback line verbatim. Plain '-n auto' uses "
        "xdist's default 'load' scheduler, which scatters same-file tests across "
        "workers and rebuilds the module-scoped whole-tree fixtures in "
        "tests/test_structural_guards.py per worker, risking wall-clock timeout "
        "flakiness on the hard 100% gate."
    )
    # Lease branch: routes the parallel run through the wrapper and takes its width
    # atoms — including the '--dist loadfile' scheduler — from the '{worker_args}'
    # placeholder the wrapper splices from the lease owner.
    assert (
        "maestro gate-lease -- $PY -m pytest tests/ -v {worker_args}" in content
    ), (
        "gate_maestro's lease branch must route the parallel core suite through "
        "'maestro gate-lease --' with the '{worker_args}' placeholder, so its "
        "worker width and the '--dist loadfile' scheduler both come from "
        "gate_capacity.worker_args (proposal 126)."
    )
    # The parallel run is gated on the flag probe, so the lease is used only when
    # '[loop] gate_lease_enabled' is on.
    assert "maestro gate-lease --enabled" in content, (
        "gate_maestro must branch the parallel run on 'maestro gate-lease "
        "--enabled' so the flag-off path stays today's behaviour."
    )


def test_gate_maestro_classifies_a_failed_parallel_run() -> None:
    """A failed lease-branch run is classified, not blindly re-run serially.

    ``--dist loadfile`` bounds fixture-rebuild cost but not peak memory, so on a
    memory-constrained runner an xdist worker can still be OOM-killed and its
    lost coverage trip ``--cov-fail-under=100`` even though every test passed
    (the failure mode that kept CI serial, PR #3385). On the lease path
    ``gate_maestro`` now pipes the captured run to ``maestro gate-classify`` and
    retries only a resource artifact (``COVERAGE`` / ``TIMEOUT`` /
    ``UNAVAILABLE``); an answered red (``FAILED`` / ``COLLECTION_ERROR``) is
    returned red with no second run. The flag-off branch keeps today's blanket
    serial fallback verbatim as the rollback contract (proposal 126, step 12).
    """
    content = _SCRIPT_FILE.read_text(encoding="utf-8")
    # Lease path classifies the failure before deciding whether to re-run.
    assert "maestro gate-classify --tool tests" in content, (
        "gate_maestro's lease branch must classify a failed parallel run through "
        "'maestro gate-classify --tool tests' rather than blindly re-running it "
        "(proposal 126, step 12)."
    )
    # The retry case arm is exactly the resource-artifact set — nothing else.
    assert _retry_arm_tokens(content) == _RETRY_TOKENS, (
        "gate_maestro must retry exactly the resource-artifact tokens "
        f"{sorted(_RETRY_TOKENS)}; an answered red must fall through to no retry."
    )
    # Rollback pin: the flag-off branch keeps today's short-circuit and its
    # blanket serial fallback verbatim, so a flag flip restores today's behaviour.
    assert "-n auto --dist loadfile && return 0" in content, (
        "gate_maestro's flag-off branch must keep the '-n auto --dist loadfile && "
        "return 0' short-circuit verbatim as the rollback pin."
    )
    assert "$PY -m pytest tests/ -v || return 3" in content, (
        "gate_maestro's flag-off branch must keep the blanket serial fallback "
        "('$PY -m pytest tests/ -v || return 3') as the rollback contract for "
        "[loop] gate_lease_enabled (PR #3385)."
    )


@pytest.mark.parametrize(
    "token,expect_retry",
    [(tok, False) for tok in sorted(_ANSWERED_RED_TOKENS)]
    + [(tok, True) for tok in sorted(_RETRY_TOKENS)],
)
def test_gate_maestro_retry_decision_per_token(token: str, expect_retry: bool) -> None:
    """Each seeded classifier token maps to retry-or-not per gate_maestro's case arm.

    Drives the branch logic over the closed classifier vocabulary without running
    the shell: a resource artifact (``COVERAGE`` / ``TIMEOUT`` / ``UNAVAILABLE``)
    earns a retry, an answered red (``FAILED`` / ``COLLECTION_ERROR``) does not.
    Reads the decision straight from the script's retry ``case`` arm so a future
    edit that widens or narrows the retry set is caught.
    """
    retries = token in _retry_arm_tokens(_SCRIPT_FILE.read_text(encoding="utf-8"))
    assert retries is expect_retry, (
        f"classifier token {token!r}: expected "
        f"{'a retry' if expect_retry else 'no retry'}, "
        f"got {'a retry' if retries else 'no retry'}."
    )


def test_gate_maestro_retry_is_bounded_width() -> None:
    """The one classified retry re-acquires the lease, so it runs at granted width.

    A resource-artifact retry must re-enter ``maestro gate-lease`` rather than
    re-running full width, so gate_maestro issues the leased core-suite command
    exactly twice — the captured parallel run and the single retry — with no bare
    full-width re-run on the lease path.
    """
    content = _SCRIPT_FILE.read_text(encoding="utf-8")
    count = content.count(_LEASED_CORE_SUITE)
    assert count == 2, (
        "gate_maestro must run the leased core-suite command exactly twice on the "
        "lease path (the captured parallel run plus one bounded-width retry); "
        f"found {count}."
    )


def test_classify_gate_run_maps_wrapper_statuses() -> None:
    """``classify_gate_run`` must map the wrapper's two non-answer statuses correctly.

    The ``maestro gate-lease`` wrapper surfaces ``127`` for a missing binary and
    ``128 + N`` for a signal death — two non-answers no transcript can carry, so
    ``classify_gate_run`` names them explicitly with ``--kind`` before handing the
    captured output to ``maestro gate-classify``. Both land in the retry class, so a
    flipped threshold (``127`` vs ``128``) or a swapped kind would silently route a
    killed-worker run to the answered-red no-retry branch — a regression the case
    arm and the CLI ``--kind`` pins can't see because neither exercises the shell
    helper. Pin the two mappings by text: ``rc == 127`` -> ``UNAVAILABLE`` and
    ``rc > 128`` -> ``TIMEOUT`` (proposal 126, step 12).
    """
    content = _SCRIPT_FILE.read_text(encoding="utf-8")
    body = re.search(r"(?ms)^classify_gate_run\(\)\s*\{(.*?)^\}", content)
    assert body is not None, (
        "classify_gate_run helper not found in the shell gate runner; the "
        "wrapper-status mapping this test pins lives in its body."
    )
    assert re.search(r"-eq\s+127\b[^\n]*then\s+kind=UNAVAILABLE", body.group(1)), (
        "classify_gate_run must map the wrapper's missing-binary status (127) to "
        "'--kind UNAVAILABLE', so a checkout without 'maestro' on PATH is retried "
        "as a resource artifact rather than treated as an answered red."
    )
    assert re.search(r"-gt\s+128\b[^\n]*then\s+kind=TIMEOUT", body.group(1)), (
        "classify_gate_run must map the wrapper's signal-death status (128 + N) to "
        "'--kind TIMEOUT', so a killed worker earns a bounded-width retry instead "
        "of an answered-red no-op."
    )


def test_command_docs_pin_loadfile_scheduler() -> None:
    """The hand-run gate docs must route the core suite through the lease.

    ``impl.md`` / ``polish.md`` / ``process-comments.md`` prescribe the core
    suite the agent runs manually — the path that actually produces simultaneous
    full-width runs. Proposal 126 routes each through ``maestro gate-lease --``
    with the ``{worker_args}`` placeholder, so the worker width *and* the
    ``--dist loadfile`` scheduler are spliced from the lease owner
    (``gate_capacity.worker_args``, pinned by ``tests/test_gate_capacity.py``)
    rather than written as literals here. Prose cannot branch on the flag, so the
    wrapper is prescribed unconditionally; with the flag off it takes no slot and
    splices today's ``-n auto --dist loadfile`` width, so the flag-off effect is
    unchanged. Each doc must therefore route its core suite through ``maestro
    gate-lease --`` via ``{worker_args}`` and name no literal worker count.
    """
    for doc in _COMMAND_DOCS:
        content = doc.read_text(encoding="utf-8")
        assert "{worker_args}" in content, (
            f"{doc.name}: the core-suite gate must route its worker atoms through "
            "the '{worker_args}' placeholder that 'maestro gate-lease' splices "
            "from the lease owner (proposal 126), so the '-n'/'--dist loadfile' "
            "pairing stays enforced at gate_capacity.worker_args rather than "
            "duplicated as a literal here."
        )
        assert "maestro.cli gate-lease --" in content, (
            f"{doc.name}: the core suite must route through 'maestro.cli gate-lease "
            "--' so its worker width comes from the execution lease (proposal "
            "126), not a hardcoded count."
        )
        assert re.search(r"-n\s*=?(auto|\d)", content) is None, (
            f"{doc.name}: the hand-run gate docs must name no literal worker "
            "count ('-n auto' / '-n <int>', spaced or combined like '-n4'); the "
            "width comes from the lease's '{worker_args}' placeholder, spliced by "
            "'maestro gate-lease'."
        )


def test_command_docs_document_classified_retry() -> None:
    """The hand-run gate docs must document classified retry, not a blanket serial re-run.

    ``scripts/quality-gates.sh gate_maestro`` no longer re-runs the core suite
    serially on any parallel failure; it classifies the failure and retries only a
    resource artifact (proposal 126, step 12). The command docs run the parallel
    core suite as a raw command the agent executes by hand, so each must give the
    same rule: classify a failed run through ``maestro gate-classify`` and re-run
    only the resource-artifact tokens, treating an answered red as a red to fix.
    A doc still telling the agent to re-run *serially* on any failure would
    re-introduce the amplifier this step removes (PR #3385 review).
    """
    for doc in _COMMAND_DOCS:
        content = doc.read_text(encoding="utf-8")
        assert "maestro.cli gate-classify" in content, (
            f"{doc.name}: must tell the agent to classify a failed core-suite run "
            "through 'maestro.cli gate-classify' instead of blindly re-running it "
            "(proposal 126, step 12)."
        )
        for token in sorted(_RETRY_TOKENS):
            assert token in content, (
                f"{doc.name}: must name the resource-artifact token {token!r} that "
                "earns a retry, so the agent re-runs only those and fixes an "
                "answered red rather than re-running blindly."
            )
        assert "scripts/quality-gates.sh gate_maestro" in content, (
            f"{doc.name}: the classified-retry note must still point at "
            "scripts/quality-gates.sh gate_maestro as the mirror so the hand-run "
            "gate path stays aligned with the script."
        )
        assert "serially" not in content, (
            f"{doc.name}: must no longer prescribe an unconditional serial re-run; "
            "the classified retry replaced the blanket serial fallback "
            "(proposal 126, step 12)."
        )


def test_ci_quality_step_pins_loadfile_scheduler() -> None:
    """The CI ``quality`` job's pytest step must pin ``--dist loadfile``.

    That job owns a single-tenant ephemeral runner container (proposal 125), so it runs the
    core suite in parallel — but plain ``-n auto`` uses xdist's default ``load``
    scheduler, scattering a file's tests across workers and rebuilding the
    module-scoped whole-tree AST fixtures in ``tests/test_structural_guards.py``
    per worker. ``--dist loadfile`` keeps a file's tests on one worker so those
    fixtures are built once, mirroring the shell runner. Pinned so a future edit
    silently dropping the scheduler flag reds here rather than reintroducing the
    flakiness class in CI.
    """
    run_text = _ci_quality_pytest_run()
    assert "-n auto --dist loadfile" in run_text, (
        "quality-gates.yml's 'Tests with coverage (pytest)' step must run the "
        "core suite with '-n auto --dist loadfile'. Plain '-n auto' falls back to "
        "xdist's default 'load' scheduler, scattering same-file tests across "
        "workers and rebuilding the module-scoped whole-tree fixtures in "
        "tests/test_structural_guards.py per worker (PR #3385). '-n auto' is the "
        "correct width on a single-tenant ephemeral runner container, so this surface is "
        "deliberately outside tests/test_gate_lease_owner.py's worker-count scan."
    )


def test_ci_quality_step_falls_back_to_serial() -> None:
    """A resource-artifact parallel failure in CI must re-run serially, not fail the gate.

    ``--dist loadfile`` bounds fixture-rebuild cost but not peak memory, so a
    worker can still be OOM-killed and its lost coverage trip
    ``--cov-fail-under=100`` even though every test passed (PR #3385). The step
    must run the parallel suite first and short-circuit green on success
    (``exit 0``), and keep a serial re-run — a bare ``pytest`` invocation with no
    worker count — as the authoritative result for a classified resource artifact
    (which arm it sits in is pinned by
    ``test_ci_quality_serial_fallback_is_classified``). The fallback must announce
    itself so a run that exercises it is visible in the log.
    """
    run_text = _ci_quality_pytest_run()
    # A green parallel run short-circuits the step before the serial fallback.
    assert "exit 0" in run_text, (
        "the CI pytest step must short-circuit green on a passing parallel run "
        "('exit 0') so the common case keeps the xdist speed-up."
    )
    # The fallback re-runs the suite serially: a `pytest "${args[@]}"` invocation
    # that names no worker count. The parallel line carries '-n auto', so a bare
    # one is the serial re-run.
    serial_lines = [
        line
        for line in run_text.splitlines()
        if 'pytest "${args[@]}"' in line and "-n" not in line
    ]
    assert serial_lines, (
        "the CI pytest step must re-run the suite serially on a parallel failure "
        "(a bare 'pytest \"${args[@]}\"' with no '-n' worker count) so an "
        "OOM-killed worker's lost coverage can't fail the 100% gate spuriously "
        "(PR #3385)."
    )
    # The fallback is announced via a workflow ::warning:: so exercising it is
    # visible without weakening the gate.
    assert "::warning::" in run_text, (
        "the CI pytest step must emit a '::warning::' when it falls back to the "
        "serial re-run, so a fallback is a visible signal to triage — not a "
        "silent reason to weaken the coverage gate."
    )


def test_ci_quality_serial_fallback_is_classified() -> None:
    """Only a resource artifact earns the CI serial re-run; an answered red fails fast.

    A blanket serial re-run repeats a genuine red at serial speed, which outran the
    job timeout and turned every real test failure into an uninformative
    ``cancelled`` run. The step must classify the failed parallel run through the
    one classifier the shell runner's lease path uses (``gate-classify``), re-run
    serially only inside the resource-artifact arm, and exit with the parallel
    run's status on any other token.
    """
    run_text = _ci_quality_pytest_run()
    assert "gate-classify --tool tests" in run_text, (
        "the CI pytest step must classify a failed parallel run with "
        "'maestro gate-classify --tool tests' (proposal 126's owner) rather than "
        "re-running the whole suite serially on any failure."
    )
    assert 'rc="${PIPESTATUS[0]}"' in run_text, (
        "the parallel run is piped through tee for classification, so its status "
        "must be read from PIPESTATUS — the runner's shell has no pipefail."
    )
    # A run that never reached its own verdict is a non-answer, mapped exactly as
    # scripts/quality-gates.sh's classify_gate_run does, so a signal-killed
    # controller (rc 137) is retried rather than failed fast.
    assert "extra=(--kind UNAVAILABLE)" in run_text, (
        "rc 127 (runner could not start) must classify via '--kind UNAVAILABLE'."
    )
    assert "extra=(--kind TIMEOUT)" in run_text, (
        "rc > 128 (signal-killed run, e.g. an OOM-killed controller) must classify "
        "via '--kind TIMEOUT' so it earns the serial re-run."
    )
    assert "kind=FAILED" in run_text, (
        "an empty classifier token must default to FAILED (fail fast, never mask)."
    )
    lines = run_text.splitlines()
    artifact_arm = next(
        (i for i, line in enumerate(lines) if line.strip() == "COVERAGE|TIMEOUT|UNAVAILABLE)"),
        None,
    )
    assert artifact_arm is not None, "missing the COVERAGE|TIMEOUT|UNAVAILABLE) case arm"
    answered_arm = next((i for i, line in enumerate(lines) if line.strip() == "*)"), None)
    assert answered_arm is not None, "missing the '*)' answered-red case arm"
    serial = next(
        (
            i
            for i, line in enumerate(lines)
            if 'pytest "${args[@]}"' in line and "-n" not in line
        ),
        None,
    )
    assert serial is not None, "missing the serial 'pytest \"${args[@]}\"' re-run"
    assert artifact_arm < serial < answered_arm, (
        "the serial re-run must sit inside the resource-artifact arm "
        "(COVERAGE|TIMEOUT|UNAVAILABLE), not run for an answered red."
    )
    assert 'exit "$rc"' in "\n".join(lines[answered_arm:]), (
        "an answered red (FAILED / COLLECTION_ERROR) must exit with the parallel "
        "run's status instead of re-running serially."
    )


def test_ci_quality_timeout_admits_the_serial_fallback() -> None:
    """The quality job's timeout leaves room for the parallel run plus the serial re-run.

    At 15 minutes the serial pass could not finish after a failed parallel run, so
    the job was cancelled before reporting. The asserted floor (30) is the sum of
    the measured ~6 min parallel run and the ~15+ min serial pass plus setup; the
    workflow sets 35 for headroom above that floor.
    """
    data = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    timeout = data["jobs"]["quality"]["timeout-minutes"]
    assert timeout >= 30, (
        f"quality timeout-minutes is {timeout}; it must be >= 30 so the "
        "classified serial re-run can conclude rather than be cancelled."
    )
