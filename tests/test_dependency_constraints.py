"""Cross-path reproducibility guard for the pinned production dependency set.

Maestro's production dependencies are resolved by three independent ``pip
install`` paths — the clean-room Docker build (``Dockerfile``), the loop
container's entrypoint sync (``scripts/docker-entrypoint.sh``), and the
self-hosted CI runner (``.github/workflows/quality-gates.yml``). An unbounded
specifier lets each land a different version, so CI can pass on the older,
working release while the freshly-built image ships a newer, incompatible one —
a silent green-CI / red-deploy split that already cost two auto-rolled-back
production deploys.

``constraints.txt`` pins the ``[project.dependencies]`` runtime set to exact
versions and is threaded through all three paths with ``-c``, so a bump is one
reviewable edit that moves every path together. These read-only structural
guards prove each path consumes the file and that the historically-divergent
``mcp`` pin stays inside the range ``pyproject.toml`` allows — turning "one
dependency, three answers" into "one dependency, one answer".
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONSTRAINTS = _REPO_ROOT / "constraints.txt"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_ENTRYPOINT = _REPO_ROOT / "scripts" / "docker-entrypoint.sh"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "quality-gates.yml"
_SAFE_DEPLOY = _REPO_ROOT / "scripts" / "safe-deploy.sh"


def _all_constraint_pins() -> list[Requirement]:
    """Every pin parsed from ``constraints.txt`` (comments and blanks skipped)."""
    pins: list[Requirement] = []
    for raw in _CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pins.append(Requirement(line))
    return pins


def _constraint_pin(name: str) -> Requirement:
    """The pin for ``name`` parsed from ``constraints.txt`` (raises if absent)."""
    for req in _all_constraint_pins():
        if req.name == name:
            return req
    raise AssertionError(f"constraints.txt does not pin {name!r}")


def _pyproject_dependency_specs() -> dict[str, Requirement]:
    """Map each ``[project.dependencies]`` package name to its parsed specifier."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return {
        Requirement(dep).name: Requirement(dep)
        for dep in data["project"]["dependencies"]
    }


def test_constraint_pin_raises_for_absent_package() -> None:
    """A package absent from constraints.txt makes the helper fail loud.

    Exercises the comment/blank skipping and the no-match ``raise`` path so a
    future edit that drops a pin surfaces as an explicit failure, not a silent
    ``None``.
    """
    with pytest.raises(AssertionError, match="does not pin"):
        _constraint_pin("not-a-real-package")


def test_constraints_file_pins_mcp_within_pyproject_spec() -> None:
    """(a) constraints.txt pins ``mcp`` with an exact ``==`` inside the spec.

    The exact pin and the ``pyproject.toml`` ``mcp`` specifier cannot silently
    conflict — a constraint outside the declared range would make ``pip``'s
    ``-c`` resolution unsatisfiable on every path at once.
    """
    assert _CONSTRAINTS.is_file(), f"expected {_CONSTRAINTS} to exist"

    mcp_pin = _constraint_pin("mcp")
    clauses = list(mcp_pin.specifier)
    assert len(clauses) == 1, f"mcp must be a single exact pin, got {mcp_pin.specifier}"
    assert clauses[0].operator == "==", (
        f"mcp must be pinned with an exact ==, got {clauses[0]}"
    )

    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    specs = [
        Requirement(dep)
        for dep in data["project"]["dependencies"]
        if Requirement(dep).name == "mcp"
    ]
    assert len(specs) == 1, "expected exactly one mcp dependency specifier"
    pinned_version = clauses[0].version
    assert specs[0].specifier.contains(pinned_version), (
        f"constraints.txt pins mcp=={pinned_version}, which is outside the "
        f"pyproject.toml spec {specs[0].specifier}"
    )


def test_dockerfile_copies_and_constrains_build_install() -> None:
    """(b) the Dockerfile COPYs constraints.txt and passes ``-c`` on its install.

    Without the ``COPY`` the file is absent from the build context and the
    ``-c`` flag would fail; without the ``-c`` flag the clean-room resolver is
    unconstrained. Both must be present.
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY constraints.txt ." in text, (
        "Dockerfile must COPY constraints.txt into the build context before the "
        "install"
    )
    assert "-c constraints.txt" in text, (
        "the Dockerfile build install must pass -c constraints.txt"
    )


def test_entrypoint_constrains_writable_install() -> None:
    """(c) the entrypoint's writable-path sync passes ``-c /workspace/constraints.txt``.

    The loop container re-resolves dependencies from the bind-mounted workspace
    on every start; without the constraint that runtime resolve can drift from
    the image the deploy validated.
    """
    text = _ENTRYPOINT.read_text(encoding="utf-8")
    assert "-c /workspace/constraints.txt" in text, (
        "the entrypoint's editable /workspace install must pass "
        "-c /workspace/constraints.txt"
    )


def _is_maestro_root_install(run: str) -> bool:
    """True iff ``run`` installs the editable root package with *any* extras.

    Matches ``pip install -e ".[<extras>]"`` for every extras form — a single
    extra (``".[dev]"`` / ``".[email]"`` / ``".[browser]"``) as well as the
    comma-separated forms — while excluding the sibling-package installs
    (``maestro_sdk/[dev]`` / ``maestro_review/[dev]``), whose ``[`` is preceded
    by ``/`` not ``.`` and which do not depend on ``mcp``. The earlier
    ``.[dev,`` discriminator matched only comma-separated extras, so a future
    single-extra maestro install would slip past the guard unconstrained.
    """
    return "pip install" in run and '-e ".[' in run


def _maestro_install_runs(data: Any = None) -> dict[str, str]:
    """Map job name -> its maestro root-package ``pip install`` step ``run`` string.

    Reads the committed workflow unless ``data`` (parsed YAML) is supplied for a
    test. Every editable root-package install with extras is selected via
    :func:`_is_maestro_root_install`, so a future single-extra maestro job is
    caught rather than silently skipped.
    """
    if data is None:
        data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    runs: dict[str, str] = {}
    for job_name, job in data["jobs"].items():
        for step in job["steps"]:
            run = step.get("run", "")
            if _is_maestro_root_install(run):
                runs[job_name] = run
    return runs


def test_ci_quality_and_browser_installs_are_constrained() -> None:
    """(d) every maestro-package CI install passes ``-c constraints.txt``.

    These are the CI steps that install the maestro production dependency set on
    a fresh ephemeral runner container, whose empty Python environment makes pip resolve every
    transitive dependency from scratch; without ``-c constraints.txt`` that
    resolve can drift off the pinned set. The known ``quality`` / ``browser-tests``
    jobs must exist, and *every* collected maestro install — not just those two —
    must be constrained, so a future third maestro-installing CI job added without
    ``-c constraints.txt`` trips this guard instead of silently reintroducing
    the divergence.
    """
    runs = _maestro_install_runs()
    for job in ("quality", "browser-tests"):
        assert job in runs, (
            f"expected a maestro-package install step in the {job!r} job"
        )
    for job_name, run in runs.items():
        assert "-c constraints.txt" in run, (
            f"the {job_name!r} job's maestro install must pass -c constraints.txt"
        )


def test_root_install_discriminator_matches_any_extra() -> None:
    """The discriminator catches single-extra maestro installs, not just comma forms.

    The earlier ``.[dev,`` substring skipped a future ``pip install -e ".[dev]"``
    (or ``".[email]"`` / ``".[browser]"``) — a single extra with no comma — so an
    unconstrained single-extra maestro install would evade the guard. The
    broadened root-package discriminator matches every extras form while still
    excluding the sibling-package installs.
    """
    for run in (
        'pip install -e ".[dev]"',
        'pip install -e ".[email]"',
        'pip install -e ".[browser]"',
        'pip install -e ".[dev,email]" -c constraints.txt',
    ):
        assert _is_maestro_root_install(run), (
            f"{run!r} should be recognised as a maestro root-package install"
        )
    for run in (
        'pip install -e "maestro_sdk/[dev]"',
        'pip install -e "maestro_review/[dev]"',
        'pip install -e "maestro_sdk/"',
    ):
        assert not _is_maestro_root_install(run), (
            f"{run!r} is a sibling-package install and must be excluded"
        )


def test_ci_guard_catches_unconstrained_single_extra_install() -> None:
    """A single-extra maestro CI install without ``-c constraints.txt`` trips the guard.

    Feeds a synthetic workflow whose lone maestro step uses one extra and omits
    the constraint. The guard collects that step (proving the broadened
    discriminator sees a single-extra install) and it carries no
    ``-c constraints.txt``, so ``test_ci_quality_and_browser_installs_are_constrained``
    would fail on it — exactly the divergence the guard exists to catch.
    """
    synthetic = {
        "jobs": {
            "future-job": {
                "steps": [{"run": 'pip install -e ".[email]"'}],
            }
        }
    }
    runs = _maestro_install_runs(synthetic)
    assert runs == {"future-job": 'pip install -e ".[email]"'}
    assert "-c constraints.txt" not in runs["future-job"]


def test_constraints_pin_every_pyproject_dependency() -> None:
    """Every ``[project.dependencies]`` package has an exact ``==`` pin.

    constraints.txt's docstring claims it pins the whole runtime dependency set,
    but only the ``mcp`` pin was previously guarded. A future pyproject
    dependency added without a matching pin would silently reopen the cross-path
    divergence for that one package; binding the two lists together turns that
    omission into an explicit failure.
    """
    specs = _pyproject_dependency_specs()
    pins = {req.name: req for req in _all_constraint_pins()}

    missing = sorted(set(specs) - set(pins))
    assert not missing, (
        f"constraints.txt is missing exact pins for {missing} declared in "
        f"pyproject.toml [project.dependencies]"
    )

    for name in specs:
        clauses = list(pins[name].specifier)
        assert len(clauses) == 1, (
            f"{name} must be a single exact pin, got {pins[name].specifier}"
        )
        assert clauses[0].operator == "==", (
            f"{name} must be pinned with an exact ==, got {clauses[0]}"
        )


def test_every_constraint_pin_sits_within_pyproject_spec() -> None:
    """Every constraints.txt pin resolves inside its ``pyproject.toml`` spec.

    A pin outside its declared range (a floor raised in pyproject but not
    reflected here, or vice versa) makes ``pip install -c`` unsatisfiable on
    every resolution path at once. Guarding only ``mcp`` left the other pins
    unchecked; this generalizes the within-spec assertion to all of them.
    """
    specs = _pyproject_dependency_specs()
    for pin in _all_constraint_pins():
        assert pin.name in specs, (
            f"constraints.txt pins {pin.name!r}, which is absent from "
            f"pyproject.toml [project.dependencies]"
        )
        clauses = list(pin.specifier)
        assert len(clauses) == 1, (
            f"{pin.name} must be a single exact pin, got {pin.specifier}"
        )
        pinned_version = clauses[0].version
        assert specs[pin.name].specifier.contains(pinned_version), (
            f"constraints.txt pins {pin.name}=={pinned_version}, which is "
            f"outside the pyproject.toml spec {specs[pin.name].specifier}"
        )


def test_jinja2_pinned_at_or_above_cve_patched_floor() -> None:
    """jinja2 is pinned to at least 3.1.6 — the clean-resolve, CVE-patched floor.

    The within-spec guards above pass for any ``jinja2 >= 3.1.0``, so they cannot
    catch a *stale* pin: 3.1.2 (April 2022) satisfied the ``jinja2>=3.1.0`` spec
    yet was a persistent-environment artifact — ``pip`` left it installed instead
    of upgrading, the exact drift this file exists to eliminate — while a
    clean-room resolve of ``jinja2>=3.1.0`` lands the latest 3.1.x. Pinning the
    stale 3.1.2 would reproducibly downgrade the image and lock in
    template-rendering CVEs (xmlattr XSS, sandbox bypasses) fixed after it, which
    matters because the dashboard renders via jinja2. This floor keeps every
    resolution path on a patched release without hard-pinning a single version.
    """
    jinja_pin = _constraint_pin("jinja2")
    clauses = list(jinja_pin.specifier)
    assert len(clauses) == 1, (
        f"jinja2 must be a single exact pin, got {jinja_pin.specifier}"
    )
    pinned = Version(clauses[0].version)
    assert pinned >= Version("3.1.6"), (
        f"jinja2 is pinned to {pinned}, below the CVE-patched 3.1.6 floor; a "
        "clean resolve of jinja2>=3.1.0 lands the latest 3.1.x, so pinning an "
        "older release reproducibly ships a vulnerable, downgraded version"
    )


def test_claude_agent_sdk_floor_at_or_above_mcp_v2_capable_release() -> None:
    """claude-agent-sdk's pyproject floor is at least 0.2.149 — the mcp-v2-capable release.

    ``0.2.149`` is the first ``claude-agent-sdk`` release whose metadata permits
    ``mcp`` 2.x *and* that dispatches against the 2.x ``Server`` surface (recorded
    in ``docs/mcp-v2-migration.md``); it is the hard prerequisite that unblocks
    the MCP v2 migration. The within-spec guards above pass for any floor
    ``>=0.2.87``, so they cannot catch a *stale* SDK floor: 0.2.87 predates the
    2.x-capable dispatch, so an unconstrained ``pip install -e .`` could resolve a
    release that crashes agent construction under ``mcp`` 2.x. Locking the floor
    here turns a regression below 0.2.149 into an explicit failure and — because
    ``test_every_constraint_pin_sits_within_pyproject_spec`` binds the
    constraints.txt pin inside this spec — keeps the exact pin from slipping back
    too.
    """
    spec = _pyproject_dependency_specs()["claude-agent-sdk"]
    floors = [
        Version(clause.version)
        for clause in spec.specifier
        if clause.operator in (">=", "==", "~=")
    ]
    assert floors, (
        f"claude-agent-sdk must declare a lower-bound floor, got {spec.specifier}"
    )
    floor = max(floors)
    assert floor >= Version("0.2.149"), (
        f"claude-agent-sdk floor is {floor}, below the mcp-v2-capable 0.2.149 "
        "release; an older SDK crashes agent construction under mcp 2.x, so a "
        "regression below this floor silently reopens the break "
        "docs/mcp-v2-migration.md gates the migration on"
    )


def test_safe_deploy_rebuild_trigger_includes_constraints() -> None:
    """(e) safe-deploy.sh's ``DEPS_CHANGED`` trigger watches ``constraints.txt``.

    A constraints-only bump changes no other watched file and does not move the
    pyproject hash, so without this the deploy would run the stale image and the
    new pin would never reach production.
    """
    text = _SAFE_DEPLOY.read_text(encoding="utf-8")
    assert "constraints\\.txt" in text, (
        "safe-deploy.sh's DEPS_CHANGED rebuild trigger must include "
        "constraints.txt so a constraints-only bump rebuilds the image"
    )
