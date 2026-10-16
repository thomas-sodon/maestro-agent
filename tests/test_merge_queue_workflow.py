"""Pin the merge-queue trigger contract of ``quality-gates.yml``.

`Quality Gates` (`.github/workflows/quality-gates.yml`) is the required-check
suite a landing must pass. For a GitHub **merge queue** to serialize landings —
building a temporary branch that stacks ``main`` plus the queued PRs and running
the required suite once against that merged tip — the suite must fire on the
queue's ``merge_group`` event. This guard pins two facts so a later edit cannot
silently drop them: ``merge_group`` is an ``on:`` trigger, and no required job
(`quality`, `browser-tests`, `package-quality`) carries an event-gating ``if:``
that would exclude a ``merge_group`` run.

The trigger is **inert** until an operator enables "Require merge queue" on
``main`` — ``merge_group`` never fires before then — but wiring it now, and
guarding it here, is the foundation increment. The ``concurrency`` shape (which
already routes ``merge_group`` to a per-``github.sha`` run-to-completion group)
is owned by `tests/test_quality_gates_concurrency.py` and is deliberately not
re-asserted here.

This module also carries the golden guard for proposal 128 step 6's retirement of
the eager PR-head resync sweep: once the merge queue made per-PR branch currency a
non-precondition, the ``auto-update.yml`` workflow (the ``autoupdate-action``
marketplace action that merged ``main`` into every open PR's head on each push)
was deleted. `_eager_resync_report` reds if any committed workflow reintroduces
that sweep, so the removal cannot silently regress.

The detector is pure over an injected workflow dict, following the ``_report``
house pattern of the sibling workflow-shape modules
(`tests/test_workload_placement.py`), with a
positive control and seeded-negative controls. PyYAML parses the unquoted ``on:``
key as the boolean ``True`` (YAML 1.1 treats ``on``/``off`` as booleans), so the
trigger lives under the ``True`` key; the ``workflow[True] if True in workflow
else workflow["on"]`` idiom handles that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"
)
_WORKFLOWS_DIR = _WORKFLOW_PATH.parent

#: The eager-resync sweep's unambiguous signature: the marketplace action that
#: merged ``main`` into every open PR's head on each push to ``main``. Proposal
#: 128 step 6 retired it once the merge queue made per-PR branch currency a
#: non-precondition; this marker fails the golden guard below if any workflow
#: reintroduces the sweep.
_EAGER_RESYNC_MARKER = "autoupdate-action"

#: The required jobs that must run against the merge queue's temporary branch.
#: A missing job here fails the guard rather than letting it pass vacuously.
_REQUIRED_JOBS = ("quality", "browser-tests", "package-quality")


def _load_workflow() -> dict[object, object]:
    """Parse the committed workflow and return its top-level mapping."""
    data = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), data
    return data


def _trigger_mapping(workflow: dict[object, object]) -> object:
    """Return the ``on:`` trigger value, honouring the YAML 1.1 ``on``→``True`` key.

    PyYAML parses an unquoted ``on:`` as the boolean ``True``, so the trigger
    lives under the ``True`` key, not the string ``"on"``.
    """
    return workflow[True] if True in workflow else workflow.get("on")


def _event_gates_out_merge_group(guard: str) -> bool:
    """Return whether an ``if:`` expression event-gates out ``merge_group`` runs.

    A required job restricted to specific events via ``github.event_name`` (e.g.
    ``github.event_name == 'pull_request'``) would skip on the merge queue's
    ``merge_group`` event unless it also names ``merge_group``. This is the
    realistic regression the guard blocks: a later edit that narrows a required
    job to PR/push events, silently excluding it from the queue's landing run.
    """
    lowered = guard.lower()
    return "event_name" in lowered and "merge_group" not in lowered


def _merge_queue_report(workflow: dict[object, object]) -> str:
    """Return a failure message for every way the merge-queue contract drifts.

    Pure over an injected workflow dict (``""`` when clean), aggregating every
    offender in one message: no ``on:`` mapping, a missing ``merge_group`` trigger, a missing/
    malformed required job, or a required job whose ``if:`` event-gates out the
    ``merge_group`` event.
    """
    problems: list[str] = []

    trigger = _trigger_mapping(workflow)
    if not isinstance(trigger, dict):
        problems.append(f"workflow has no `on:` trigger mapping: {trigger!r}")
    elif "merge_group" not in trigger:
        problems.append(
            "`merge_group` is not an `on:` trigger, so the required suite never "
            "fires on the merge queue's temporary branch (triggers: "
            f"{sorted(map(str, trigger))})"
        )

    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        problems.append(f"workflow has no `jobs` mapping: {jobs!r}")
    else:
        for name in _REQUIRED_JOBS:
            job = jobs.get(name)
            if not isinstance(job, dict):
                problems.append(f"required job {name!r} is missing or malformed")
                continue
            guard = job.get("if")
            if isinstance(guard, str) and _event_gates_out_merge_group(guard):
                problems.append(
                    f"required job {name!r} carries an event-gating `if:` that "
                    f"excludes the merge_group event: {guard!r}"
                )

    if not problems:
        return ""
    return "quality-gates.yml merge-queue drift: " + "; ".join(problems) + "."


def _eager_resync_report(workflow_texts: dict[str, str]) -> str:
    """Return a failure message for every workflow that reintroduces the sweep.

    Pure over injected ``{name: raw_text}`` (``""`` when clean), following the
    ``_report`` house pattern. A committed workflow whose text references the
    ``autoupdate-action`` marketplace action — the eager ``push:[main]``-triggered
    PR-head resync sweep proposal 128 step 6 retired — reds, so a later edit
    cannot silently re-add the branch-mutation storm the merge queue replaced.
    """
    offenders = sorted(
        name
        for name, text in workflow_texts.items()
        if _EAGER_RESYNC_MARKER in text.lower()
    )
    if not offenders:
        return ""
    return (
        "Workflows reintroduce the retired eager PR-head resync sweep (the "
        f"{_EAGER_RESYNC_MARKER!r} marketplace action): "
        + ", ".join(offenders)
        + ". Proposal 128 step 6 removed auto-update.yml once the merge queue made "
        "per-PR branch currency a non-precondition; do not re-add it."
    )


def _workflow(
    *,
    trigger: Any = None,
    jobs: Any = None,
    on_key: object = True,
) -> dict[object, object]:
    """Build an in-memory workflow doc keyed like PyYAML's parse of ``on:``.

    Defaults to the clean shape (``merge_group`` present, every required job
    present and un-gated). ``on_key`` defaults to the boolean ``True`` the real
    parse yields; pass ``"on"`` to exercise the string-key branch of
    :func:`_trigger_mapping`.
    """
    if trigger is None:
        trigger = {
            "push": {"branches": ["main"]},
            "pull_request": {"branches": ["main"]},
            "merge_group": None,
            "workflow_dispatch": None,
        }
    if jobs is None:
        jobs = {name: {"runs-on": "ubuntu-latest"} for name in _REQUIRED_JOBS}
    return {on_key: trigger, "jobs": jobs}


# --- Real-tree callers ------------------------------------------------------


def _committed_workflow_texts() -> dict[str, str]:
    """Map each committed ``.github/workflows/*.yml`` file to its raw text.

    The non-empty assertion mirrors the sibling repo-tree guards
    (`tests/test_workload_placement.py`): a glob resolving nothing would let the
    golden guard below pass having scanned no workflow.
    """
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(_WORKFLOWS_DIR.glob("*.yml"))
    }
    assert texts, (
        f"No workflows found under {_WORKFLOWS_DIR}; the glob matched nothing, so "
        "the eager-resync golden guard would pass without scanning anything."
    )
    return texts


def test_no_workflow_reintroduces_eager_resync_sweep() -> None:
    """No committed workflow re-adds the retired eager PR-head resync sweep.

    Proposal 128 step 6 deleted ``auto-update.yml`` once the merge queue made
    per-PR branch currency a non-precondition. This golden guard reds if any
    workflow reintroduces the ``autoupdate-action`` sweep.
    """
    report = _eager_resync_report(_committed_workflow_texts())
    assert report == "", report


def test_real_workflow_declares_merge_group_trigger() -> None:
    """The committed workflow lists ``merge_group`` among its ``on:`` triggers."""
    trigger = _trigger_mapping(_load_workflow())
    assert isinstance(trigger, dict), trigger
    assert "merge_group" in trigger


def test_real_workflow_satisfies_the_merge_queue_contract() -> None:
    """The committed workflow yields no merge-queue drift report."""
    report = _merge_queue_report(_load_workflow())
    assert report == "", report


# --- Detector arms (in-memory fixtures) -------------------------------------


def test_detector_accepts_a_merge_queue_workflow() -> None:
    """Positive control: the clean in-memory shape yields no report."""
    assert _merge_queue_report(_workflow()) == ""


def test_detector_reads_string_on_key() -> None:
    """A workflow keyed under the string ``"on"`` still resolves its trigger.

    Exercises the ``else`` branch of :func:`_trigger_mapping` (the real parse
    always yields the ``True`` key).
    """
    assert _merge_queue_report(_workflow(on_key="on")) == ""


def test_detector_accepts_a_guard_that_names_merge_group() -> None:
    """A required job whose ``if:`` names ``merge_group`` is not flagged.

    Covers the ``_event_gates_out_merge_group`` False path for a job that does
    carry an event guard but keeps the merge_group event in scope.
    """
    jobs = {name: {"runs-on": "x"} for name in _REQUIRED_JOBS}
    jobs["quality"]["if"] = (
        "${{ github.event_name == 'merge_group' "
        "|| github.event_name == 'pull_request' }}"
    )
    assert _merge_queue_report(_workflow(jobs=jobs)) == ""


def test_detector_reds_when_merge_group_trigger_missing() -> None:
    """Seeded negative: an ``on:`` mapping without ``merge_group`` reds."""
    trigger = {"push": {"branches": ["main"]}, "pull_request": {"branches": ["main"]}}
    report = _merge_queue_report(_workflow(trigger=trigger))
    assert report != ""
    assert "merge_group" in report


def test_detector_reds_when_no_trigger_mapping() -> None:
    """Seeded negative: a non-mapping ``on:`` value reds without crashing."""
    report = _merge_queue_report(_workflow(trigger="whenever"))
    assert report != ""
    assert "trigger mapping" in report


def test_detector_reds_when_no_jobs_mapping() -> None:
    """Seeded negative: a workflow with no ``jobs`` mapping reds."""
    report = _merge_queue_report({True: {"merge_group": None}})
    assert report != ""
    assert "jobs" in report


def test_detector_reds_when_required_job_missing() -> None:
    """Seeded negative: a missing required job reds and names the job."""
    jobs = {name: {"runs-on": "x"} for name in _REQUIRED_JOBS}
    del jobs["browser-tests"]
    report = _merge_queue_report(_workflow(jobs=jobs))
    assert report != ""
    assert "browser-tests" in report


def test_detector_reds_when_required_job_event_gated_out() -> None:
    """Seeded negative: a required job gated to non-merge_group events reds.

    A job narrowed to ``github.event_name == 'pull_request'`` would skip on the
    queue's ``merge_group`` event — exactly the regression this guard blocks.
    """
    jobs = {name: {"runs-on": "x"} for name in _REQUIRED_JOBS}
    jobs["quality"]["if"] = "${{ github.event_name == 'pull_request' }}"
    report = _merge_queue_report(_workflow(jobs=jobs))
    assert report != ""
    assert "quality" in report
    assert "merge_group" in report


# --- Eager-resync golden guard (in-memory fixtures) -------------------------


def test_eager_resync_detector_accepts_clean_workflows() -> None:
    """Positive control: workflows with no sweep marker yield no report."""
    texts = {
        "quality-gates.yml": "on:\n  merge_group:\n  push:\n    branches: [main]\n",
        "deploy.yml": "on:\n  workflow_run:\n",
    }
    assert _eager_resync_report(texts) == ""


def test_eager_resync_detector_reds_on_the_sweep_marker() -> None:
    """Seeded negative: a workflow re-adding the autoupdate action reds and names it.

    This is the regression the golden guard exists to block — a later edit that
    reintroduces the ``push:[main]``-triggered PR-head resync sweep the merge
    queue replaced.
    """
    texts = {
        "auto-update.yml": (
            "on:\n  push:\n    branches: [main]\n"
            "    uses: docker://chinthakagodawita/autoupdate-action@sha256:deadbeef\n"
        )
    }
    report = _eager_resync_report(texts)
    assert report != ""
    assert "auto-update.yml" in report
    assert _EAGER_RESYNC_MARKER in report


# --- Event-gate helper (direct unit coverage) -------------------------------


def test_event_gate_helper_flags_a_non_merge_group_event_guard() -> None:
    """An ``event_name`` guard that omits ``merge_group`` is flagged."""
    assert _event_gates_out_merge_group("github.event_name == 'pull_request'") is True


def test_event_gate_helper_allows_a_guard_that_names_merge_group() -> None:
    """An ``event_name`` guard that names ``merge_group`` is allowed."""
    assert _event_gates_out_merge_group("github.event_name == 'merge_group'") is False


def test_event_gate_helper_allows_a_non_event_guard() -> None:
    """A guard that does not reference ``event_name`` at all is allowed."""
    assert _event_gates_out_merge_group("always()") is False
