"""Enforce a single Node-runtime floor for the CI actions every workflow pins.

Every CI job that runs Python does so through two first-party GitHub Actions —
``actions/checkout`` and ``actions/setup-python`` — whose Node runtime is an
implicit property of the action's major version: ``checkout@v4`` /
``setup-python@v5`` run on Node 20, while ``checkout@v5`` / ``setup-python@v6``
run on Node 24. GitHub forces the Node 24 runtime by default from 2026-06-02 and
removes Node 20 in September 2026; once Node 20 is gone a below-floor pin stops
working, the **Quality Gates** workflow goes red, and — because the autonomous
deploy fires only on a green Quality Gates run and branch protection requires
green CI to merge — the loop can no longer land any PR or deploy.

This module is the single owner of "what Node runtime CI requires". It declares
one floor (``MIN_NODE_RUNTIME``), maps every pinned first-party ref to the Node
runtime its major bundles (``ACTION_NODE_RUNTIME``, including the below-floor
majors so a stale pin resolves to a real runtime *under* the floor rather than
to ``None``), and walks every committed workflow plus every committed
``.github/actions/**/action.{yml,yaml}`` composite — asserting every action pin
meets the floor. The repo's only ``setup-python`` pin and Python version now
live in one such composite (``.github/actions/py-setup``), so that sweep is
load-bearing rather than forward-cover, and this module also owns the
single-source property: exactly one ``setup-python`` pin, under
``.github/actions/``, and exactly one ``python-version``. A below-floor pin is
now a red build instead of a surprise outage, and the next Node 24 → 26 wave
becomes a one-line ``MIN_NODE_RUNTIME`` bump that names every stale pin.

Like the sibling repo-tree guard in ``tests/test_workload_placement``, the table and resolver are self-contained
here (not in ``maestro/``), the real-tree callers read committed artifacts
through a real ``Path``, and every guard branch is exercised against in-memory
dict-literal fixtures that are never written to disk.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_ACTIONS_DIR = _REPO_ROOT / ".github" / "actions"
#: GitHub accepts both extensions for a workflow file, so both are swept — a
#: workflow added as ``.yaml`` must not slip past the floor.
_WORKFLOW_GLOBS = ("*.yml", "*.yaml")
#: Composite actions live one directory deep under ``.github/actions/<name>/``
#: and, like workflows, may use either extension.
_COMPOSITE_GLOBS = ("**/action.yml", "**/action.yaml")

#: Workflows committed today. ``_committed_workflows`` already fails loudly on an
#: empty glob; this pins the remaining rot — a workflow dropping out of the sweep
#: (renamed, moved, or its extension changed) while its siblings keep the glob
#: non-empty, which would leave its action pins bound to nothing.
_KNOWN_WORKFLOWS = frozenset({"quality-gates.yml"})

#: Composite actions committed today, keyed by their directory name under
#: ``.github/actions/<name>/`` (every composite file is ``action.yml``, so the
#: directory is its identity). ``py-setup`` owns the repo's only ``setup-python``
#: pin and Python version. ``_committed_composite_actions`` fails loudly on an
#: empty glob; this pins the remaining rot — the composite dropping out of the
#: sweep (renamed, moved, or its extension changed) while a sibling keeps the
#: glob non-empty, which would silently drop that pin from the floor.
_KNOWN_COMPOSITE_ACTIONS = frozenset({"py-setup"})

#: The Node runtime each pinned first-party action major bundles. The
#: below-floor majors (``@v4`` / ``@v5``) are kept alongside the current ones so
#: a stale pin resolves to a real runtime *under* the floor and reds explicitly,
#: rather than resolving to ``None`` and being skipped as if exempt.
#:
#: The keys match the *whole* ``uses:`` ref, so any new *first-party* pin shape —
#: a patch/minor tag (``@v5.0.1``) or a floating ``@main`` — stays unmapped until
#: added here as its own reviewed row. That is deliberate: it couples this
#: runtime-currency axis to the separate immutability axis at the point a human
#: introduces a new ``actions/…`` ref, so such a pin lands with a matching runtime
#: assertion rather than silently bypassing the floor. A ``docker://`` container
#: ref is a different case:
#: ``resolve_action_node_runtime`` short-circuits on the ``docker://`` prefix
#: *before* this table is consulted, so it is exempt automatically and needs no
#: row here — it runs no first-party Node runtime the floor governs.
ACTION_NODE_RUNTIME: dict[str, int] = {
    "actions/checkout@v4": 20,
    "actions/checkout@v5": 24,
    "actions/checkout@v7": 24,
    "actions/setup-python@v5": 20,
    "actions/setup-python@v6": 24,
    "actions/setup-python@v7": 24,
    # Third-party: setup-uv publishes no floating major tag, so py-setup pins
    # the exact release and each bump lands here as its own reviewed row.
    "astral-sh/setup-uv@v10.2.0": 24,
}

#: The minimum Node runtime a CI action pin may run on. Node 20 is removed from
#: the GitHub-Actions runners in September 2026; 24 is the current default.
MIN_NODE_RUNTIME = 24

#: A representative ``docker://…@sha256:`` digest ref, used only as sample input
#: to the ``docker://`` exemption branches below. Its *exact* value is irrelevant:
#: ``resolve_action_node_runtime`` short-circuits on the ``docker://`` prefix
#: before the table lookup, so *any* ``docker://`` ref is exempt and needs no
#: ``ACTION_NODE_RUNTIME`` row. The repo currently pins no ``docker://`` action, so
#: this is a synthetic sample kept only to exercise that exemption branch.
_SAMPLE_DOCKER_DIGEST_REF = (
    "docker://example/some-action@sha256:"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


class UnmappedActionError(Exception):
    """A non-exempt ``uses:`` ref is absent from ``ACTION_NODE_RUNTIME``.

    Raised so a newly added first-party or third-party Node action cannot slip
    past the floor by being silently omitted from the table — the walk reds
    until the ref is mapped to the Node runtime its major bundles.
    """


def resolve_action_node_runtime(uses_ref: str | None) -> int | None:
    """Return the Node runtime a step's ``uses:`` ref runs on, or ``None`` if exempt.

    Exempt (``None``): a step with no ``uses:`` (a shell-only ``run:`` step), a
    ``docker://…`` container action, and a local ``./…`` composite reference —
    none of which runs on a first-party action's bundled Node runtime this floor
    governs. A local composite's *own* action pins are swept separately via
    ``_committed_composite_actions``, so exempting the call site keeps the
    documented forward-cover working instead of reding a moved pin. A ref present
    in ``ACTION_NODE_RUNTIME`` resolves to its runtime. Any other action ref — a
    first-party ``actions/…`` or a third-party Node action absent from the table
    — raises ``UnmappedActionError`` so the walk reds rather than skipping it
    silently.
    """
    if uses_ref is None:
        return None
    ref = uses_ref.strip()
    if not ref:
        return None
    if ref.startswith("docker://"):
        return None
    if ref.startswith("./"):
        return None
    runtime = ACTION_NODE_RUNTIME.get(ref)
    if runtime is None:
        raise UnmappedActionError(
            f"{ref!r} is not in ACTION_NODE_RUNTIME. Add it with the Node "
            "runtime its major version bundles so the floor can enforce it."
        )
    return runtime


def _iter_steps(name: str, doc: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(location, step)`` for every step in a workflow *or* composite doc.

    A workflow file carries steps under ``jobs.<job>.steps``; a composite action
    file (``.github/actions/**/action.yml``) carries them under ``runs.steps``.
    Both shapes are walked so an action pin — or a ``python-version`` — moved into
    a composite stays covered. The full step dict is yielded so callers can read
    ``uses`` (the runtime floor) *and* ``with`` (the single-source detector).
    """
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job_name, job in sorted(jobs.items()):
            if not isinstance(job, dict):
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            for index, step in enumerate(steps):
                if isinstance(step, dict):
                    yield f"{name}:{job_name}[{index}]", step
    runs = doc.get("runs")
    if isinstance(runs, dict):
        steps = runs.get("steps")
        if isinstance(steps, list):
            for index, step in enumerate(steps):
                if isinstance(step, dict):
                    yield f"{name}:runs[{index}]", step


def _iter_action_uses(name: str, doc: dict[str, Any]) -> Iterator[tuple[str, str | None]]:
    """Yield ``(location, uses)`` for every step in a workflow *or* composite doc.

    A thin projection of :func:`_iter_steps` reading each step's ``uses:``. A step
    with no ``uses:`` yields ``None`` — a shell-only ``run:`` step — which the
    resolver exempts.
    """
    for location, step in _iter_steps(name, doc):
        yield location, step.get("uses")


def _below_floor_report(docs: dict[str, dict[str, Any]]) -> str:
    """Return a failure message for every action pin below ``MIN_NODE_RUNTIME``.

    Pure over injected ``{name: doc}`` data. Exempt refs (``None``) are skipped;
    an unmapped ref propagates ``UnmappedActionError`` from the resolver so the
    table can never silently omit a newly added action.
    """
    offenders: list[str] = []
    for name, doc in sorted(docs.items()):
        for location, uses in _iter_action_uses(name, doc):
            runtime = resolve_action_node_runtime(uses)
            if runtime is None:
                continue
            if runtime < MIN_NODE_RUNTIME:
                offenders.append(
                    f"{location} pins {uses!r}, which runs on Node {runtime} "
                    f"(below the floor of {MIN_NODE_RUNTIME})"
                )
    if not offenders:
        return ""
    return (
        "CI action pins run on a Node runtime below "
        f"MIN_NODE_RUNTIME={MIN_NODE_RUNTIME}: "
        + "; ".join(offenders)
        + ". Bump each to a major whose runtime is at or above the floor "
        "(e.g. actions/checkout@v5, actions/setup-python@v6) and add the new "
        "ref to ACTION_NODE_RUNTIME."
    )


def _python_setup_single_source_report(docs: dict[str, dict[str, Any]]) -> str:
    """Return a failure message when the Python setup is not single-sourced.

    Pure over injected ``{name: doc}`` data, where each ``name`` is the doc's
    repo-relative path. Two properties define single-sourcing, each its own arm:

    * every ``uses:`` ref beginning ``actions/setup-python`` must occur exactly
      once across every workflow and composite, and that one occurrence must live
      under ``.github/actions/`` (the composite), never inlined in a workflow job;
    * every ``with.python-version`` declaration must likewise occur exactly once.

    A workflow job that re-inlines ``setup-python``, a second composite carrying
    its own pin, or a job that re-declares ``python-version`` all red this, so the
    twelve occurrences this increment collapsed to two cannot silently drift back
    apart.
    """
    setup_python_locations: list[str] = []
    version_locations: list[str] = []
    for name, doc in sorted(docs.items()):
        for location, step in _iter_steps(name, doc):
            uses = step.get("uses")
            if isinstance(uses, str) and uses.strip().startswith("actions/setup-python"):
                setup_python_locations.append(location)
            with_block = step.get("with")
            if isinstance(with_block, dict) and "python-version" in with_block:
                version_locations.append(location)
    problems: list[str] = []
    if len(setup_python_locations) != 1:
        problems.append(
            "expected exactly one actions/setup-python pin, found "
            f"{len(setup_python_locations)}: {setup_python_locations}"
        )
    elif not setup_python_locations[0].split(":", 1)[0].startswith(".github/actions/"):
        problems.append(
            "the sole actions/setup-python pin must live under .github/actions/ "
            f"(the composite), not inlined at {setup_python_locations[0]}"
        )
    if len(version_locations) != 1:
        problems.append(
            "expected exactly one with.python-version declaration, found "
            f"{len(version_locations)}: {version_locations}"
        )
    if not problems:
        return ""
    return "Python setup is not single-sourced: " + "; ".join(problems) + "."


def _committed_workflows() -> list[Path]:
    """Return every committed workflow file (``.yml`` and ``.yaml``), sorted by name.

    Both extensions are swept because GitHub accepts either. The non-empty
    assertion lives here so every caller inherits it: globs resolving nothing — a
    moved directory, a renamed extension — would otherwise let the floor sweep
    pass having validated zero action pins.
    """
    paths = sorted(
        path for pattern in _WORKFLOW_GLOBS for path in _WORKFLOWS_DIR.glob(pattern)
    )
    assert paths, (
        f"Expected at least one {' / '.join(_WORKFLOW_GLOBS)} under "
        f"{_WORKFLOWS_DIR}; the globs matched nothing, so the runtime-floor sweep "
        "would pass without checking any action pin."
    )
    return paths


def _committed_composite_actions() -> list[Path]:
    """Return every committed composite ``action.yml`` / ``action.yaml``, sorted by path.

    The repo's only ``setup-python`` pin and Python version now live in the
    ``.github/actions/py-setup`` composite, swept here for its own action pins
    without any further wiring. Both extensions are globbed because GitHub accepts
    either. The non-empty assertion is load-bearing: an empty glob no longer means
    "nothing to check" but that the sole ``setup-python`` pin has silently left
    the floor — so, like ``_committed_workflows``, this fails loudly rather than
    passing having validated zero pins.
    """
    paths = sorted(
        path for pattern in _COMPOSITE_GLOBS for path in _ACTIONS_DIR.glob(pattern)
    )
    assert paths, (
        f"Expected at least one {' / '.join(_COMPOSITE_GLOBS)} under "
        f"{_ACTIONS_DIR}; the globs matched nothing. The repo's only "
        "setup-python pin lives in a composite action, so an empty sweep means "
        "that pin silently left the runtime floor."
    )
    return paths


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse a workflow or composite-action file into its top-level mapping.

    Only ``jobs`` / ``runs`` are read downstream, which sidesteps a PyYAML quirk:
    an unquoted ``on:`` key parses as the boolean ``True`` under YAML 1.1, so
    triggers do not live under the string key ``"on"``.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (path.name, data)
    return data


def _load_action_docs() -> dict[str, dict[str, Any]]:
    """Map each committed workflow and composite action to its parsed mapping."""
    docs: dict[str, dict[str, Any]] = {}
    for path in _committed_workflows():
        docs[str(path.relative_to(_REPO_ROOT))] = _load_yaml(path)
    for path in _committed_composite_actions():
        docs[str(path.relative_to(_REPO_ROOT))] = _load_yaml(path)
    return docs


# --- Real-tree callers ------------------------------------------------------


def test_sweep_covers_the_known_workflows() -> None:
    """The sweep still reaches every workflow committed today.

    ``_committed_workflows`` already fails loudly on an empty glob, so this
    guards the remaining rot: a workflow dropping out of the scanned set — moved,
    renamed, or its extension changed — while its siblings keep the glob
    non-empty, which would silently drop its action pins from the floor.
    """
    scanned = {path.name for path in _committed_workflows()}
    missing = _KNOWN_WORKFLOWS - scanned
    assert not missing, (
        f"Known committed workflows are missing from the sweep: {sorted(missing)}. "
        "Either the workflow was removed (drop it from _KNOWN_WORKFLOWS) or the "
        "glob no longer resolves .github/workflows."
    )


def test_sweep_covers_the_known_composite_actions() -> None:
    """The sweep still reaches every composite action committed today.

    ``_committed_composite_actions`` fails loudly on an empty glob, so this
    guards the remaining rot: the ``py-setup`` composite — the repo's sole
    ``setup-python`` pin — dropping out of the scanned set (moved, renamed, or
    its extension changed) while a sibling composite keeps the glob non-empty,
    which would silently drop that pin from the floor.
    """
    scanned = {path.parent.name for path in _committed_composite_actions()}
    missing = _KNOWN_COMPOSITE_ACTIONS - scanned
    assert not missing, (
        f"Known committed composite actions are missing from the sweep: "
        f"{sorted(missing)}. Either the composite was removed (drop it from "
        "_KNOWN_COMPOSITE_ACTIONS) or the glob no longer resolves .github/actions."
    )


def test_real_tree_meets_the_node_runtime_floor() -> None:
    """Every action pin across every committed workflow meets the floor.

    Reds if any live pin resolves to a Node runtime below ``MIN_NODE_RUNTIME``
    (a stale ``@v4`` / ``@v5`` major) or names an action absent from the table.
    """
    report = _below_floor_report(_load_action_docs())
    assert report == "", report


def test_real_tree_python_setup_is_single_sourced() -> None:
    """The repo pins ``setup-python`` and its Python version in exactly one place.

    Both live in the ``.github/actions/py-setup`` composite; if a workflow job
    re-inlines the step or re-declares the version, or a second composite carries
    its own pin, the twelve-to-two collapse has drifted back apart and this reds.
    """
    report = _python_setup_single_source_report(_load_action_docs())
    assert report == "", report


# --- Resolver branches (pure function, asserted in isolation) ---------------


def test_resolve_shell_only_step_is_exempt() -> None:
    """A step with no ``uses:`` (a ``run:`` step) resolves to ``None``."""
    assert resolve_action_node_runtime(None) is None


def test_resolve_blank_ref_is_exempt() -> None:
    """An empty or whitespace-only ref resolves to ``None``, never unmapped."""
    assert resolve_action_node_runtime("") is None
    assert resolve_action_node_runtime("   ") is None


def test_resolve_docker_container_ref_is_exempt() -> None:
    """A ``docker://…`` container action resolves to ``None`` (no Node runtime)."""
    assert resolve_action_node_runtime(_SAMPLE_DOCKER_DIGEST_REF) is None


def test_resolve_local_composite_ref_is_exempt() -> None:
    """A local ``./…`` composite reference resolves to ``None``, never unmapped.

    A ``uses: ./.github/actions/py-setup`` step delegates to a composite action
    whose own pins ``_committed_composite_actions`` sweeps; the call site has no
    first-party Node runtime of its own, so it is exempt rather than reding with
    ``UnmappedActionError``.
    """
    assert resolve_action_node_runtime("./.github/actions/py-setup") is None


def test_resolve_below_floor_majors_return_their_runtime() -> None:
    """A stale ``@v4`` / ``@v5`` major resolves to Node 20, not ``None``."""
    assert resolve_action_node_runtime("actions/checkout@v4") == 20
    assert resolve_action_node_runtime("actions/setup-python@v5") == 20


def test_resolve_current_majors_meet_the_floor() -> None:
    """The bumped ``@v5`` / ``@v6`` majors resolve to Node 24."""
    assert resolve_action_node_runtime("actions/checkout@v5") == 24
    assert resolve_action_node_runtime("actions/setup-python@v6") == 24


def test_resolve_unmapped_action_raises() -> None:
    """A non-exempt ref absent from the table raises ``UnmappedActionError``."""
    with pytest.raises(UnmappedActionError):
        resolve_action_node_runtime("actions/upload-artifact@v4")


# --- Walker guard branches (in-memory dict-literal fixtures) -----------------


def test_below_floor_pin_reds() -> None:
    """Negative control: a below-floor ``@v4`` pin is named in the report."""
    docs = {
        "quality-gates.yml": {
            "jobs": {"quality": {"steps": [{"uses": "actions/checkout@v4"}]}}
        }
    }
    report = _below_floor_report(docs)
    assert "actions/checkout@v4" in report
    assert "quality-gates.yml:quality[0]" in report


def test_current_pins_are_clean() -> None:
    """Positive control: floor-meeting pins yield no report."""
    docs = {
        "quality-gates.yml": {
            "jobs": {
                "quality": {
                    "steps": [
                        {"uses": "actions/checkout@v5"},
                        {"uses": "actions/setup-python@v6"},
                    ]
                }
            }
        }
    }
    assert _below_floor_report(docs) == ""


def test_docker_and_shell_only_steps_are_skipped_not_failed() -> None:
    """A ``docker://`` ref and a shell-only ``run:`` step are exempt, not offenders."""
    docs = {
        "sample.yml": {
            "jobs": {
                "sample": {
                    "steps": [
                        {"name": "build", "run": "echo hi"},
                        {"uses": _SAMPLE_DOCKER_DIGEST_REF},
                    ]
                }
            }
        }
    }
    assert _below_floor_report(docs) == ""


def test_unmapped_action_reds_the_walk() -> None:
    """An unmapped first-party Node action propagates ``UnmappedActionError``."""
    docs = {
        "quality-gates.yml": {
            "jobs": {"quality": {"steps": [{"uses": "actions/upload-artifact@v4"}]}}
        }
    }
    with pytest.raises(UnmappedActionError):
        _below_floor_report(docs)


def test_composite_action_doc_resolves() -> None:
    """A composite ``action.yml``'s ``runs.steps`` are walked, not just ``jobs``.

    A composite (``.github/actions/**/action.yml``, e.g. ``py-setup``) carries its
    pins under ``runs``, not ``jobs``; a floor-meeting pin there is clean and a
    below-floor one reds, so the pin the composite now owns stays covered.
    """
    clean = {
        "action.yml": {
            "runs": {"using": "composite", "steps": [{"uses": "actions/setup-python@v6"}]}
        }
    }
    assert _below_floor_report(clean) == ""
    stale = {
        "action.yml": {
            "runs": {"using": "composite", "steps": [{"uses": "actions/setup-python@v5"}]}
        }
    }
    report = _below_floor_report(stale)
    assert "actions/setup-python@v5" in report
    assert "action.yml:runs[0]" in report


# --- Single-source detector (in-memory dict-literal fixtures) -----------------


def _single_source_docs() -> dict[str, dict[str, Any]]:
    """The clean state: the ``py-setup`` composite owns the only pin and version.

    Keys are repo-relative paths so the detector's "under .github/actions/" arm
    reads a realistic location. Jobs call the composite via ``./…`` and never
    re-declare the pin or the version — the positive control.
    """
    return {
        ".github/actions/py-setup/action.yml": {
            "runs": {
                "using": "composite",
                "steps": [
                    {
                        "uses": "actions/setup-python@v7",
                        "with": {"python-version": "3.12"},
                    }
                ],
            }
        },
        ".github/workflows/quality-gates.yml": {
            "jobs": {
                "quality": {
                    "steps": [
                        {"uses": "actions/checkout@v7"},
                        {"uses": "./.github/actions/py-setup"},
                        {"run": "pytest"},
                    ]
                }
            }
        },
    }


def test_single_source_positive_control() -> None:
    """Positive control: one composite pin, jobs calling it, yields no report."""
    assert _python_setup_single_source_report(_single_source_docs()) == ""


def test_reinlined_setup_python_reds() -> None:
    """A workflow job that re-inlines ``actions/setup-python`` reds (two pins)."""
    docs = _single_source_docs()
    docs[".github/workflows/quality-gates.yml"]["jobs"]["quality"]["steps"].append(
        {"uses": "actions/setup-python@v7", "with": {"python-version": "3.12"}}
    )
    report = _python_setup_single_source_report(docs)
    assert "actions/setup-python" in report


def test_second_composite_pin_reds() -> None:
    """A second composite carrying its own ``setup-python`` pin reds (two pins)."""
    docs = _single_source_docs()
    docs[".github/actions/py-setup-2/action.yml"] = {
        "runs": {
            "using": "composite",
            "steps": [
                {"uses": "actions/setup-python@v7", "with": {"python-version": "3.12"}}
            ],
        }
    }
    report = _python_setup_single_source_report(docs)
    assert "actions/setup-python" in report


def test_sole_pin_inlined_in_workflow_reds() -> None:
    """Exactly one pin, but inlined in a workflow job rather than the composite, reds.

    The count arm passes (one occurrence); the location arm reds because the sole
    pin must live under ``.github/actions/``.
    """
    docs = {
        ".github/workflows/quality-gates.yml": {
            "jobs": {
                "quality": {
                    "steps": [
                        {
                            "uses": "actions/setup-python@v7",
                            "with": {"python-version": "3.12"},
                        }
                    ]
                }
            }
        }
    }
    report = _python_setup_single_source_report(docs)
    assert ".github/actions/" in report


def test_redeclared_python_version_reds() -> None:
    """A second ``with.python-version`` declaration reds even with one pin.

    Isolates the version arm: the composite still holds the only ``setup-python``
    pin (its count/location arm stays clean), but a workflow job re-declares
    ``python-version``, so the version arm reds on its own.
    """
    docs = _single_source_docs()
    docs[".github/workflows/quality-gates.yml"]["jobs"]["quality"]["steps"].append(
        {"name": "Re-declare version", "with": {"python-version": "3.12"}}
    )
    report = _python_setup_single_source_report(docs)
    assert "python-version" in report
