"""Regression: Quality Gates must not cancel in-progress runs on ``main``.

`Quality Gates` (`.github/workflows/quality-gates.yml`) runs on both
``push: [main]`` and ``pull_request``. Its workflow-level concurrency group is
keyed on ``github.ref``, which is ``refs/heads/main`` for *every* main push, so
back-to-back main commits collide in one group. An unconditional
``cancel-in-progress: true`` therefore lets a second main push cancel the first
run, leaving that commit with no verdict of its own (issue #1249).

The fix scopes cancellation to pull-request events, so PR iteration stays fast
while every push to ``main`` runs to completion and carries its own verdict. This guard asserts the corrected configuration so a future edit that
re-introduces unconditional cancellation on ``main`` fails the gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"
)


def _load_concurrency() -> dict[str, object]:
    """Parse the workflow and return its top-level ``concurrency`` mapping."""
    data = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    concurrency = data["concurrency"]
    assert isinstance(concurrency, dict), concurrency
    return concurrency


def _load_jobs() -> dict[str, object]:
    """Parse the workflow and return its ``jobs`` mapping."""
    data = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert isinstance(jobs, dict), jobs
    return jobs


def _fans_in_on_quality(job: dict[str, Any]) -> bool:
    """True iff ``job`` declares a ``needs`` dependency on the ``quality`` gate.

    Both the list form (``needs: [quality]``) and the bare-string form
    (``needs: quality``) fan the job in, so membership over ``needs`` — defaulting
    to an empty list when absent — catches either. The independence guards below
    route through this predicate, and
    ``test_independence_guard_rejects_a_re_added_fan_in`` feeds it a synthetic
    offender, so a predicate that silently stopped biting reds there rather than
    letting the absence assertions pass vacuously.
    """
    return "quality" in job.get("needs", [])


# A concrete xdist worker count as spelled on a pytest command line: ``-n auto``
# / ``-n 4`` as two tokens, or the combined ``-n4`` / ``-n=auto`` form. Mirrors
# the ``-n\s*=?(auto|\d)`` shape ``tests/test_gate_lease_owner.py`` uses so the
# two guards read a worker count the same way.
_WORKER_COUNT_RE = re.compile(r"-n\s*=?(auto|\d)")


def _quality_pytest_run(data: Any = None) -> str:
    """Return the ``quality`` job's single ``run:`` string that invokes pytest.

    Builds on :func:`_load_jobs` by default, or accepts a pre-parsed workflow
    document via ``data`` (the injection shape of
    ``tests/test_dependency_constraints.py``'s ``_maestro_install_runs``) so the
    selector itself is testable. Walks the job's steps and returns the one whose
    ``run`` invokes pytest; a renamed or deleted test step raises loudly with a
    named message rather than letting the parallel pin below pass vacuously.
    """
    jobs = _load_jobs() if data is None else data["jobs"]
    quality = jobs["quality"]
    assert isinstance(quality, dict), quality
    steps = quality["steps"]
    assert isinstance(steps, list), steps
    for step in steps:
        assert isinstance(step, dict), step
        run = step.get("run")
        if isinstance(run, str) and "pytest" in run:
            return run
    raise AssertionError(
        "no pytest step in the 'quality' job of quality-gates.yml; a renamed or "
        "deleted 'Tests with coverage (pytest)' step must red this pin rather "
        "than let it pass vacuously"
    )


def _runs_in_parallel(run: str) -> bool:
    """True iff a pytest command names an xdist worker count and pins ``loadfile``.

    Both halves are required: a bare ``-n auto`` without ``--dist loadfile`` falls
    back to xdist's default ``load`` scheduler, so the pin below rejects it.
    """
    return bool(_WORKER_COUNT_RE.search(run)) and "--dist loadfile" in run


def test_cancel_in_progress_is_not_unconditionally_true() -> None:
    """An unconditional ``cancel-in-progress: true`` parses to Python ``True``.

    On ``push: [main]`` that cancels the older run, skipping its deploy. The
    fix replaces it with a GitHub Actions expression, so the value must not be
    the boolean ``True``.
    """
    concurrency = _load_concurrency()
    assert concurrency["cancel-in-progress"] is not True


def test_cancel_in_progress_is_gated_on_pull_request() -> None:
    """Cancellation is scoped to pull-request events.

    The gating expression must match the corrected form exactly. A mere
    substring check on ``pull_request`` would also pass the *inverted*
    expression ``${{ github.event_name != 'pull_request' }}`` — which cancels
    in-progress runs on ``push: [main]`` and so re-introduces #1249 — so the
    guard pins the full expression to catch a one-character ``==``→``!=`` edit.
    """
    concurrency = _load_concurrency()
    cancel = concurrency["cancel-in-progress"]
    assert isinstance(cancel, str)
    assert cancel == "${{ github.event_name == 'pull_request' }}"


def test_concurrency_group_is_per_pr_ref_and_per_main_sha() -> None:
    """The group key is per-PR-ref for PRs and per-commit-SHA for main pushes.

    A shared ``github.ref`` group on ``main`` lets GitHub cancel a *pending*
    run when a newer commit queues (regardless of ``cancel-in-progress``),
    leaving that commit undeployed (#1249). The expression therefore keys on
    ``github.sha`` for non-PR events so every main commit gets a unique group
    and runs to completion. Pinned exactly so a regression cannot collapse it
    back to a shared ``github.ref`` group.
    """
    concurrency = _load_concurrency()
    group = concurrency["group"]
    assert isinstance(group, str)
    assert group == (
        "quality-gates-"
        "${{ github.event_name == 'pull_request' && github.ref || github.sha }}"
    )


def test_browser_tests_job_runs_independently() -> None:
    """The heavy ``browser-tests`` job must *not* fan in behind the ``quality`` gate.

    The three jobs (``quality``, ``browser-tests``, ``package-quality``) each run
    on their own ephemeral GitHub-hosted runner (one per job), so the old
    ``needs: [quality]`` fan-in bought nothing real — it existed only to ration
    the single self-hosted runner slot that used to be the bottleneck, and with
    a runner per job it merely delayed the
    browser suite behind the fast gate for no dependency reason. Dropping it lets all three jobs start concurrently,
    cutting wall-clock time to full CI feedback; the deliberate trade is that the
    browser suite now also runs when ``quality`` is red, spending some extra
    billed minutes to learn all failures in one pass. Asserted via :func:`_fans_in_on_quality` so a
    re-added ``needs: [quality]`` reds the gate.
    """
    jobs = _load_jobs()
    browser = jobs["browser-tests"]
    assert isinstance(browser, dict), browser
    assert not _fans_in_on_quality(browser)


def test_package_quality_job_runs_independently() -> None:
    """The merged ``package-quality`` job must *not* fan in behind the ``quality`` gate.

    The former ``review-quality`` and ``sdk-quality`` jobs are collapsed into one
    ``package-quality`` job that installs both sibling packages once and runs both
    gate sets in sequence. Neither sibling (``maestro_review``, ``maestro_sdk``)
    even depends on the ``maestro`` package the ``quality`` job
    lint/type/test-checks, so fanning it in behind ``quality`` served only the
    retired single-runner-slot scarcity. It runs on its own ephemeral
    single-tenant VM, so dropping the ``needs`` clause lets it start concurrently
    with the other two jobs — the same deliberate billed-minutes-for-wall-clock
    trade as ``browser-tests``. Asserted via :func:`_fans_in_on_quality` so a
    re-added ``needs: [quality]`` reds the gate.
    """
    jobs = _load_jobs()
    package_quality = jobs["package-quality"]
    assert isinstance(package_quality, dict), package_quality
    assert not _fans_in_on_quality(package_quality)


def test_independence_guard_rejects_a_re_added_fan_in() -> None:
    """Non-vacuity: a job that re-declares ``needs: [quality]`` reds the predicate.

    The two guards above assert an *absence* — ``not _fans_in_on_quality(...)`` —
    which would pass vacuously if the predicate silently stopped detecting the
    ``needs`` clause. Feeding :func:`_fans_in_on_quality` a synthetic offender
    that fans in on ``quality`` (mirroring the seeded-offender discipline of
    ``test_parallel_pin_rejects_a_serial_pytest_command``) asserts the predicate
    still bites, so a re-added fan-in reds the gate rather than slipping through.
    """
    offender = {"needs": ["quality"], "runs-on": "ubuntu-latest"}
    assert _fans_in_on_quality(offender)


def test_package_quality_job_runs_both_sibling_gate_sets() -> None:
    """The merged job must carry *both* packages' gate sets, not silently one.

    Collapsing the two former jobs into one risks a future edit dropping one
    package's steps while the independence guard above — which only inspects the
    job's ``needs`` clause, not which package steps it runs — still passes. This
    characterization asserts the merged job runs ``maestro_review``'s pytest
    (``tests/test_maestro_review`` in a ``run:`` string) *and* ``maestro_sdk``'s
    pytest (a ``working-directory: maestro_sdk`` step running ``pytest tests/``),
    so dropping either package's gates reds here.
    """
    jobs = _load_jobs()
    package_quality = jobs["package-quality"]
    assert isinstance(package_quality, dict), package_quality
    steps = package_quality["steps"]
    assert isinstance(steps, list), steps

    runs_review_pytest = any(
        isinstance(step, dict)
        and isinstance(step.get("run"), str)
        and "tests/test_maestro_review" in step["run"]
        and "pytest" in step["run"]
        for step in steps
    )
    runs_sdk_pytest = any(
        isinstance(step, dict)
        and step.get("working-directory") == "maestro_sdk"
        and isinstance(step.get("run"), str)
        and step["run"].startswith("pytest tests/")
        for step in steps
    )
    assert runs_review_pytest, steps
    assert runs_sdk_pytest, steps


def test_ci_core_suite_runs_in_parallel() -> None:
    """The ``quality`` job runs the core suite under pytest-xdist with ``loadfile``.

    The job owns an ephemeral GitHub-hosted runner (``runs-on: ubuntu-latest``)
    off the production host, so the co-tenant OOM-kill that once justified
    a serial run is gone; restoring a worker flag roughly halves the core suite's
    wall clock. That matters more now the three jobs run in parallel rather than
    fanning in behind ``quality``: the core suite sits on the critical path to
    full CI feedback rather than gating two jobs queued behind it.
    ``--dist loadfile`` is mandatory, not decorative: it keeps a file's tests on
    one worker so ``tests/test_structural_guards.py``'s module-scoped whole-tree
    AST fixtures are built once per worker instead of rebuilt by every worker
    under xdist's default ``load`` scheduler — which, combined with the wall-clock
    ``timeout = 5`` (``timeout_method = "thread"``), is what pushes the
    budget-tight structural-guard and fault-seeding cases past their budget. So
    the pin asserts *both* a worker flag and the scheduler flag, not merely the
    former.

    The workflow is deliberately kept out of ``tests/support/gate_sites.py``:
    ``tests/test_gate_lease_owner.py`` forbids any worker-count literal at the
    eight *shared-host* gate sites it scans from that module, so listing the
    workflow there would turn this step's ``-n auto`` into a guard violation. That
    guard governs admission on a shared host where several runs collide; CI is
    single-tenant per job, has no peers to negotiate with, and is correct to claim
    the whole runner — so the omission is a decision, not an oversight.
    """
    assert _runs_in_parallel(_quality_pytest_run())


def test_parallel_pin_rejects_a_serial_pytest_command() -> None:
    """Non-vacuity: a bare serial pytest ``run`` fails the parallel predicate.

    Feeds the accessor a synthetic ``quality`` job whose test step runs pytest
    with no worker flag, mirroring the seeded-offender discipline of
    ``tests/test_gate_command_owner.py`` and ``tests/test_overlay_safety.py`` so a
    predicate that silently stopped biting would red here.
    """
    serial = {
        "jobs": {
            "quality": {"steps": [{"name": "Tests", "run": "pytest tests/ -v"}]}
        }
    }
    assert not _runs_in_parallel(_quality_pytest_run(serial))


def test_parallel_pin_requires_the_loadfile_scheduler_flag() -> None:
    """Non-vacuity for the ``--dist loadfile`` half of the predicate.

    ``test_parallel_pin_rejects_a_serial_pytest_command`` isolates the
    worker-count half by dropping ``-n``, but nothing else drops *only* the
    scheduler pin, so a regression to
    ``return bool(_WORKER_COUNT_RE.search(run))`` — dropping the
    ``and "--dist loadfile" in run`` conjunct — would pass every other case in
    this module (the real workflow carries both flags; the serial control
    carries neither). This control keeps the worker flag but omits
    ``--dist loadfile`` so the predicate must reject it, biting exactly that
    regression.
    """
    assert not _runs_in_parallel("pytest tests/ -v -n auto")


def test_accessor_fails_loudly_when_no_pytest_step() -> None:
    """A ``quality`` job with no pytest step raises rather than passing vacuously.

    Covers the accessor's loud-failure branch, so a renamed or deleted test step
    reds this module instead of leaving the parallel pin asserting over nothing.
    """
    no_pytest = {
        "jobs": {
            "quality": {
                "steps": [{"name": "Lint", "run": "ruff check maestro/ tests/"}]
            }
        }
    }
    with pytest.raises(AssertionError, match="no pytest step"):
        _quality_pytest_run(no_pytest)
