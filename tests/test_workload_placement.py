"""Regression: every CI job's runner is one ``deploy/placement.toml`` declares.

`deploy/placement.toml` is the checked-in statement of which long-lived workload
runs on which machine tier, and which runner label routes a workflow job to
which pool. Nothing enforced it. Any job declaration across
`.github/workflows/` could be edited back to ``self-hosted`` — pointing CI at
the production host the migration moved it off — with nothing turning red.

These tests close that surface at zero cost. They bind every ``runs-on:`` across
every workflow to a ``runner_label`` declared in the manifest, pin the
manifest's own vocabulary (``class`` drawn from a closed set, ``tier``
declared), and red when an ``active`` ``bursty-batch`` workload shares a tier
with an ``interactive`` *or* a ``steady-autonomous`` one without a written
``colocation_exception``. Both starvable classes are named deliberately: an
invariant covering only ``interactive`` would leave the two Maestro containers
— the steady-autonomous tenants CI's all-cores burst actually starves —
checked by nothing.

A second set of checks binds the checked-in systemd resource drop-ins under
``deploy/systemd/`` to the same manifest: every bounded ``systemd`` workload
(class other than ``incidental``) must carry a drop-in with an explicit
``CPUWeight`` and a finite ``MemoryMax``, the interactive tier must be weighted
strictly above the batch runners, and no orphan drop-in directory may survive a
manifest entry's rename or deletion.

Each check is a pure detector over injected ``(manifest, workflows)`` or
``(manifest, dropins)`` data plus a thin caller reading the real tree, with a
seeded negative control per guard arm. Coverage measures only ``maestro/`` and
``maestro_review/``, so an unreachable branch here is flagged by nothing else.
Like the sibling repo-tree guards in ``tests/test_overlay_safety`` and
``tests/test_no_line_citations_in_docs``, they read committed artifacts through
a real ``Path`` rather than ``mem_fs``.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = _REPO_ROOT / "deploy" / "placement.toml"
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_WORKFLOW_GLOB = "*.yml"

#: Workflows committed today. The loader below already fails loudly on an empty
#: glob; this pins the remaining rot — a workflow dropping out of the sweep
#: while its siblings keep the glob non-empty, which would leave its ``runs-on:``
#: bound to nothing.
_KNOWN_WORKFLOWS = frozenset({"quality-gates.yml"})

#: The closed vocabulary of workload classes. A typo'd or invented class would
#: otherwise slip past the co-tenancy rule silently, since that rule only ever
#: asks whether a class *is* one of these.
_WORKLOAD_CLASSES = frozenset(
    {"interactive", "steady-autonomous", "bursty-batch", "incidental"}
)

#: The class whose spiky, all-cores-when-it-runs draw starves its neighbours.
_STARVING_CLASS = "bursty-batch"

#: The classes it starves — both named, per the module docstring.
_STARVABLE_CLASSES = frozenset({"interactive", "steady-autonomous"})

#: ``status`` is optional in the manifest and defaults to this. A ``retired``
#: entry records a de-registered tree still consuming disk, so it is not a live
#: co-tenant and does not need an exception.
_DEFAULT_STATUS = "active"

#: Only ``systemd`` host units carry a resource drop-in; ``container`` and
#: ``managed`` workloads have no host unit on this box to bound.
_SYSTEMD_KIND = "systemd"

#: The interactive tier, weighted strictly above the bursty-batch runners so a
#: CI burst cannot starve the operator's own shell.
_INTERACTIVE_CLASS = "interactive"

#: The one systemd class deliberately left un-evicted and unbounded — the
#: low-draw hobby services — so it is exempt from
#: the drop-in requirement. Every other declared systemd workload must be bounded.
_EXEMPT_DROPIN_CLASS = "incidental"

#: The checked-in drop-in tree, mirroring ``/etc/systemd/system/<unit>.d/``.
_DROPINS_DIR = _REPO_ROOT / "deploy" / "systemd"

#: The compose file the manifest's container ``cpu_limit`` / ``memory_limit``
#: mirror; it stays authoritative.
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"


def _load_manifest() -> dict[str, Any]:
    """Parse ``deploy/placement.toml`` and return its top-level mapping.

    Uses ``tomllib`` directly — the repo-wide idiom — rather than routing
    through ``maestro.toml_config``'s private ``_read_toml``, which swallows
    read errors this guard must surface.
    """
    data = tomllib.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), data
    return data


def _committed_workflows() -> list[Path]:
    """Return every committed workflow file, sorted by name.

    The non-empty assertion lives here rather than in one sibling test so every
    caller inherits it: a glob resolving nothing — a moved directory, a renamed
    extension — would otherwise let the sweep below pass having validated zero
    ``runs-on:`` declarations.
    """
    paths = sorted(_WORKFLOWS_DIR.glob(_WORKFLOW_GLOB))
    assert paths, (
        f"Expected at least one {_WORKFLOW_GLOB} file at {_WORKFLOWS_DIR}; "
        "the glob matched nothing, so the runner sweep would pass without "
        "validating anything."
    )
    return paths


def _load_workflows() -> dict[str, dict[str, Any]]:
    """Map each committed workflow's filename to its parsed top-level mapping.

    Only the ``jobs`` mapping is read downstream, which sidesteps a PyYAML
    quirk: an unquoted ``on:`` key parses as the boolean ``True`` under YAML 1.1,
    so triggers do not live under the string key ``"on"``.
    """
    workflows: dict[str, dict[str, Any]] = {}
    for path in _committed_workflows():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), (path.name, data)
        workflows[path.name] = data
    return workflows


def _runs_on_labels(workflows: dict[str, dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Return every ``(workflow, job, label)`` triple declared across ``workflows``.

    ``runs-on`` accepts a bare string or a list of strings; any other shape —
    notably the runner-group mapping ``{group: ..., labels: [...]}`` — cannot be
    resolved against a flat ``runner_label`` set, so it fails loudly here rather
    than being silently skipped into a green result.
    """
    triples: list[tuple[str, str, str]] = []
    for workflow_name, workflow in sorted(workflows.items()):
        jobs = workflow.get("jobs", {})
        assert isinstance(jobs, dict), (workflow_name, jobs)
        for job_name, job in sorted(jobs.items()):
            assert isinstance(job, dict), (workflow_name, job_name, job)
            if "runs-on" not in job:
                # A job that calls a reusable workflow (``uses:``) declares no
                # runner of its own; the called workflow declares it instead.
                continue
            runs_on = job["runs-on"]
            if isinstance(runs_on, str):
                labels = [runs_on]
            elif isinstance(runs_on, list) and all(isinstance(item, str) for item in runs_on):
                labels = list(runs_on)
            else:
                raise AssertionError(
                    f"{workflow_name}:{job_name} declares an unsupported "
                    f"'runs-on' shape {runs_on!r}. Only a label string or a "
                    "list of label strings can be resolved against "
                    f"{_MANIFEST_PATH.name}'s declared runner labels."
                )
            triples.extend((workflow_name, str(job_name), label) for label in labels)
    return triples


def _exception_reason(workload: dict[str, Any]) -> str:
    """Return a workload's written co-tenancy reason, or ``""`` if it has none.

    Every malformed shape collapses to ``""`` — an absent key, a non-mapping
    exception, an absent or non-string ``reason``, and a blank or whitespace-only
    one — so a co-tenancy can never be licensed by an exception that says
    nothing.
    """
    exception = workload.get("colocation_exception")
    if not isinstance(exception, dict):
        return ""
    reason = exception.get("reason")
    if not isinstance(reason, str):
        return ""
    return reason.strip()


def _vocabulary_report(manifest: dict[str, Any]) -> str:
    """Return a failure message for every workload with an unknown class or tier.

    Pure over an injected manifest rather than reading the file itself, so the
    offender branch — dead for as long as the manifest stays well-formed — is
    exercised by the seeded controls below.
    """
    declared_tiers = {tier["name"] for tier in manifest["tier"]}
    offenders: list[str] = []
    for workload in manifest["workload"]:
        name = workload["name"]
        if workload["class"] not in _WORKLOAD_CLASSES:
            offenders.append(
                f"{name}: class {workload['class']!r} is not one of "
                f"{sorted(_WORKLOAD_CLASSES)}"
            )
        if workload["tier"] not in declared_tiers:
            offenders.append(
                f"{name}: tier {workload['tier']!r} is not declared; "
                f"declared tiers are {sorted(declared_tiers)}"
            )
    if not offenders:
        return ""
    return (
        f"deploy/{_MANIFEST_PATH.name} declares workloads with an unknown class "
        "or tier: " + "; ".join(offenders) + "."
    )


def _undeclared_runner_report(
    manifest: dict[str, Any], workflows: dict[str, dict[str, Any]]
) -> str:
    """Return a failure message for every ``runs-on`` label the manifest omits."""
    declared_labels = {
        workload["runner_label"]
        for workload in manifest["workload"]
        if "runner_label" in workload
    }
    offenders = [
        f"{workflow_name}:{job_name} targets {label!r}"
        for workflow_name, job_name, label in _runs_on_labels(workflows)
        if label not in declared_labels
    ]
    if not offenders:
        return ""
    return (
        "Workflow jobs target runner labels that "
        f"deploy/{_MANIFEST_PATH.name} does not declare: "
        + "; ".join(offenders)
        + f". Declared labels are {sorted(declared_labels)}. Either target a "
        "declared runner or add the workload — with its tier and class — to "
        f"deploy/{_MANIFEST_PATH.name}."
    )


def _colocation_report(manifest: dict[str, Any]) -> str:
    """Return a failure message for every undeclared batch/latency co-tenancy.

    A tier hosting an ``interactive`` or ``steady-autonomous`` workload may not
    also host an ``active`` ``bursty-batch`` one unless that batch entry carries
    a ``colocation_exception`` with a non-empty ``reason``.
    """
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for workload in manifest["workload"]:
        by_tier.setdefault(workload["tier"], []).append(workload)

    offenders: list[str] = []
    for tier_name, entries in sorted(by_tier.items()):
        starvable = sorted(
            entry["name"]
            for entry in entries
            if entry["class"] in _STARVABLE_CLASSES
            and entry.get("status", _DEFAULT_STATUS) == _DEFAULT_STATUS
        )
        if not starvable:
            continue
        for entry in entries:
            if entry["class"] != _STARVING_CLASS:
                continue
            if entry.get("status", _DEFAULT_STATUS) != _DEFAULT_STATUS:
                continue
            if _exception_reason(entry):
                continue
            offenders.append(
                f"{entry['name']} (bursty-batch) shares tier {tier_name!r} with "
                f"{starvable}"
            )
    if not offenders:
        return ""
    return (
        "Bursty batch workloads are co-tenanted with interactive or "
        "steady-autonomous ones without a written exception: "
        + "; ".join(offenders)
        + f". Move the batch workload to its own tier in deploy/{_MANIFEST_PATH.name}, "
        "or declare a colocation_exception whose reason records why it must stay."
    )


def _misplaced_ci_report(
    manifest: dict[str, Any], workflows: dict[str, dict[str, Any]]
) -> str:
    """Return a failure message for every CI job on a shared/production tier.

    This is the guard that actually delivers the manifest's stated purpose:
    "pointing a workflow back at the production host is a red build naming this
    file." ``_undeclared_runner_report`` alone cannot — ``self-hosted`` is a
    *declared* runner label (the deploy runners), so a job edited from the
    CI pool back to ``self-hosted`` resolves fine and reds nothing there.

    So this resolves each ``runs-on:`` label to its workload's tier and reds
    when that tier also hosts an ``interactive`` or ``steady-autonomous``
    workload — the co-tenancy the migration exists to end — unless the
    ``"<workflow>:<job>"`` pair is listed in
    ``[ci_placement].production_host_jobs``. A job on a tier with no starvable
    neighbour (the dedicated CI pool) never reds; an undeclared label is
    ``_undeclared_runner_report``'s to name, so it is skipped here.
    """
    label_tier = {
        workload["runner_label"]: workload["tier"]
        for workload in manifest["workload"]
        if "runner_label" in workload
    }
    shared_tiers = {
        workload["tier"]
        for workload in manifest["workload"]
        if workload["class"] in _STARVABLE_CLASSES
        and workload.get("status", _DEFAULT_STATUS) == _DEFAULT_STATUS
    }
    allowed = set(manifest.get("ci_placement", {}).get("production_host_jobs", []))

    offenders: list[str] = []
    for workflow_name, job_name, label in _runs_on_labels(workflows):
        tier = label_tier.get(label)
        if tier is None:
            # An undeclared label is _undeclared_runner_report's to name.
            continue
        if tier not in shared_tiers:
            continue
        if f"{workflow_name}:{job_name}" in allowed:
            continue
        offenders.append(
            f"{workflow_name}:{job_name} targets {label!r} on shared tier "
            f"{tier!r}"
        )
    if not offenders:
        return ""
    return (
        "CI jobs target a runner co-tenanted with interactive or "
        "steady-autonomous work (the production host) without a placement "
        "exception: "
        + "; ".join(offenders)
        + ". Point the job at the dedicated CI pool, or add its "
        f"'<workflow>:<job>' to [ci_placement].production_host_jobs in "
        f"deploy/{_MANIFEST_PATH.name} with the structural reason it must stay."
    )


def _redundant_host_runner_report(manifest: dict[str, Any]) -> str:
    """Return a failure message when a latency-sensitive tier carries more than
    one active ``bursty-batch`` runner.

    The additive-sprawl guard behind the ``obsidian`` / ``obsidian-2``
    consolidation: one serialized deploy job needs exactly one self-hosted
    runner, so re-registering a second production-host runner — a tier already
    hosting ``interactive`` or ``steady-autonomous`` work gaining a second active
    ``bursty-batch`` tenant — reds the build naming the manifest. A tier with no
    starvable neighbour (the dedicated CI pool) is exempt: multiple batch workloads
    there starve nothing. Only ``active`` entries count, mirroring
    ``_colocation_report``, so a ``retired`` tree still consuming disk is not a
    live co-tenant; and at zero co-tenanted runners the report stays empty, so a
    later move of the deploy trigger off the host does not have to loosen it.
    """
    by_tier: dict[str, list[dict[str, Any]]] = {}
    for workload in manifest["workload"]:
        by_tier.setdefault(workload["tier"], []).append(workload)

    offenders: list[str] = []
    for tier_name, entries in sorted(by_tier.items()):
        starvable = [
            entry["name"]
            for entry in entries
            if entry["class"] in _STARVABLE_CLASSES
            and entry.get("status", _DEFAULT_STATUS) == _DEFAULT_STATUS
        ]
        if not starvable:
            continue
        batch = sorted(
            entry["name"]
            for entry in entries
            if entry["class"] == _STARVING_CLASS
            and entry.get("status", _DEFAULT_STATUS) == _DEFAULT_STATUS
        )
        if len(batch) <= 1:
            continue
        offenders.append(
            f"tier {tier_name!r} carries {len(batch)} active bursty-batch "
            f"runners co-tenanted with latency-sensitive work: {batch}"
        )
    if not offenders:
        return ""
    return (
        "More than one active bursty-batch runner is co-tenanted with "
        "interactive or steady-autonomous work (the production host): "
        + "; ".join(offenders)
        + ". One serialized deploy job needs exactly one self-hosted runner; "
        "de-register the extra runner and delete its workload from "
        f"deploy/{_MANIFEST_PATH.name}."
    )


def _cpu_weight_value(value: Any) -> int | None:
    """Return an explicit positive ``CPUWeight`` as an int, or ``None``.

    Mirrors ``_exception_reason``'s collapse-every-malformed-shape precedent: a
    missing key, a blank or non-numeric string, a non-positive number, and a
    stray ``bool`` all collapse to ``None`` — an unset weight, never a silent
    zero — so the drop-in requirement can never be satisfied by a weight that
    says nothing. The ASCII gate is load-bearing, not cosmetic: ``str.isdigit``
    is ``True`` for non-ASCII digits, but a superscript like ``"²"`` then raises
    ``ValueError`` in ``int`` (breaking the collapse-to-None contract), while
    Arabic-Indic ``"٤٠٠"`` would parse to ``400`` though systemd rejects it. Both
    must collapse to ``None`` instead.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


#: systemd's accepted ``MemoryMax`` size grammar: a plain byte count, an
#: integer/decimal with an optional binary ``K``/``M``/``G``/``T``/``P``/``E``
#: suffix, or a percentage. ``re.ASCII`` mirrors ``_cpu_weight_value``'s ASCII
#: gate so a non-ASCII digit is rejected rather than silently parsed. The
#: ``infinity`` sentinel is handled separately (it is never a *finite* ceiling),
#: so it is deliberately unmatched here.
_MEMORY_MAX_RE = re.compile(r"\d+%|\d+(?:\.\d+)?[KMGTPE]?", re.ASCII)


def _is_finite_memory_max(value: Any) -> bool:
    """Return whether ``MemoryMax`` is a present, finite, well-formed ceiling.

    ``infinity`` is systemd's unbounded sentinel — the very state this increment
    replaces — so it collapses to ``False`` alongside an absent or blank value.
    Beyond rejecting the sentinel, the value must parse as a systemd size
    (``_MEMORY_MAX_RE``): a malformed ceiling such as ``2GG`` or ``banana`` —
    which systemd rejects, leaving the unit unbounded or failing to load — is
    ``False`` too, so this guard is as strict as ``_cpu_weight_value`` rather
    than admitting every non-``infinity`` string.
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text or text.lower() == "infinity":
        return False
    return _MEMORY_MAX_RE.fullmatch(text) is not None


def _manifest_unit_name(workload: dict[str, Any]) -> str:
    """Return the systemd unit name for a manifest workload.

    The manifest records runner entries without the ``.service`` suffix
    (``actions.runner.…obsidian``); the host units carry it (``devshell.service``).
    Append it when absent so both resolve to a real ``<unit>.d`` directory.
    """
    name: str = workload["name"]
    if name.endswith(".service"):
        return name
    return f"{name}.service"


def _bounded_systemd_workloads(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every manifest workload that must carry a bounded drop-in.

    A ``systemd`` unit whose class is not ``incidental``: the container and
    managed workloads have no host unit to bound, and the incidental services
    are deliberately left unbounded. A ``retired`` entry — a de-registered tree
    still consuming disk, per the manifest's documented status semantics — is
    excluded too: it owns no live host unit, so demanding a drop-in for it would
    red the build on an impossible bound, and a lingering drop-in for it is dead
    config ``_orphan_dropin_report`` names rather than a live bound. This mirrors
    the retired-entry skip the sibling ``_colocation_report`` /
    ``_misplaced_ci_report`` detectors already apply.
    """
    return [
        workload
        for workload in manifest["workload"]
        if workload.get("kind") == _SYSTEMD_KIND
        and workload["class"] != _EXEMPT_DROPIN_CLASS
        and workload.get("status", _DEFAULT_STATUS) == _DEFAULT_STATUS
    ]


def _missing_dropin_report(
    manifest: dict[str, Any], dropins: dict[str, dict[str, str]]
) -> str:
    """Return a failure message for every bounded systemd unit lacking a bound.

    For each ``systemd`` workload whose class is not ``incidental``, require a
    parsed drop-in with an explicit positive ``CPUWeight`` and a finite
    ``MemoryMax``. An absent drop-in, a blank or non-numeric weight, or an
    ``infinity`` ceiling each name the offending unit.
    """
    offenders: list[str] = []
    for workload in _bounded_systemd_workloads(manifest):
        unit = _manifest_unit_name(workload)
        settings = dropins.get(unit)
        if settings is None:
            offenders.append(f"{unit}: no drop-in under deploy/systemd/{unit}.d/")
            continue
        problems: list[str] = []
        if _cpu_weight_value(settings.get("CPUWeight")) is None:
            problems.append(
                f"CPUWeight {settings.get('CPUWeight')!r} is not an explicit "
                "positive integer"
            )
        if not _is_finite_memory_max(settings.get("MemoryMax")):
            problems.append(
                f"MemoryMax {settings.get('MemoryMax')!r} is not a finite ceiling"
            )
        if problems:
            offenders.append(f"{unit}: " + "; ".join(problems))
    if not offenders:
        return ""
    return (
        "Bounded systemd workloads lack a resource drop-in with an explicit "
        "CPUWeight and a finite MemoryMax: "
        + "; ".join(offenders)
        + ". Add deploy/systemd/<unit>.d/10-placement.conf, or — for a service "
        "deliberately left unbounded — mark its class 'incidental' in "
        f"deploy/{_MANIFEST_PATH.name}."
    )


def _orphan_dropin_report(manifest: dict[str, Any], present_units: set[str]) -> str:
    """Return a failure message for every drop-in dir with no manifest workload.

    The inverse of ``_missing_dropin_report``: a checked-in
    ``deploy/systemd/<unit>.d`` directory that no *bounded* systemd workload in
    the manifest claims is dead config — the residue of a renamed or deleted
    entry, which ``_load_dropins`` (walking only manifest workloads) would
    silently ignore. A directory matching a container, managed, or ``incidental``
    unit is an orphan too, since none of those own a drop-in.
    """
    bounded = {
        _manifest_unit_name(workload)
        for workload in _bounded_systemd_workloads(manifest)
    }
    offenders = sorted(present_units - bounded)
    if not offenders:
        return ""
    return (
        "Drop-in directories under deploy/systemd/ match no bounded systemd "
        f"workload in deploy/{_MANIFEST_PATH.name}: "
        + "; ".join(f"{unit}.d" for unit in offenders)
        + ". Remove the stale directory, or add the workload (kind = 'systemd', "
        "class other than 'incidental') to the manifest."
    )


def _batch_outranks_interactive_report(
    manifest: dict[str, Any], dropins: dict[str, dict[str, str]]
) -> str:
    """Return a failure message when batch is not weighted below interactive.

    The "resource isolation is inverted" fix made a guarded invariant: the
    minimum ``CPUWeight`` across interactive systemd workloads must strictly
    exceed the maximum across bursty-batch ones. A unit with no drop-in or a
    malformed weight is ``_missing_dropin_report``'s to name and is skipped
    here; with no interactive or no batch drop-in there is nothing to compare.

    Steady-autonomous workloads are deliberately outside this rank: they are the
    Maestro containers (``kind = "container"``), which carry no systemd drop-in,
    so their share is the cgroup default weight (100) plus the hard quota
    ``docker-compose.yml`` sets. Their protection is that the batch runners' drop-in
    weight is held below that default (see ``deploy/systemd/``), not a rule here.
    """
    interactive: list[int] = []
    batch: list[int] = []
    for workload in manifest["workload"]:
        if workload.get("kind") != _SYSTEMD_KIND:
            continue
        settings = dropins.get(_manifest_unit_name(workload))
        if settings is None:
            continue
        weight = _cpu_weight_value(settings.get("CPUWeight"))
        if weight is None:
            continue
        if workload["class"] == _INTERACTIVE_CLASS:
            interactive.append(weight)
        elif workload["class"] == _STARVING_CLASS:
            batch.append(weight)
    if not interactive or not batch:
        return ""
    min_interactive = min(interactive)
    max_batch = max(batch)
    if min_interactive > max_batch:
        return ""
    return (
        "Interactive systemd workloads are not weighted strictly above "
        f"bursty-batch ones: minimum interactive CPUWeight {min_interactive} "
        f"does not exceed maximum batch CPUWeight {max_batch}. Raise the "
        "interactive drop-in's CPUWeight or lower the batch one in "
        "deploy/systemd/."
    )


# --- Real-tree callers ------------------------------------------------------


def _parse_dropin_conf(text: str) -> dict[str, dict[str, str]]:
    """Parse a systemd drop-in ``.conf`` into ``{section: {key: value}}``.

    A two-key hand-rolled reader: the repo carries no ``configparser`` (whose
    ``optionxform`` would fold ``CPUWeight``'s case), and this file already
    deliberately parses TOML with ``tomllib`` and YAML with ``yaml``. Blank
    lines and ``#`` / ``;`` comments are skipped; a ``[Section]`` header opens a
    section; a ``key=value`` line populates the current one, key kept verbatim.
    A ``key=value`` line before any header, or one with no ``=``, fails loudly
    rather than being silently dropped.
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip(), {})
            continue
        assert current is not None, f"key=value before any [Section]: {raw!r}"
        assert "=" in line, f"malformed drop-in line (no '='): {raw!r}"
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return sections


def _merge_service_settings(conf_texts: Iterable[str]) -> dict[str, str]:
    """Merge the ``[Service]`` sections of drop-in confs in order; later wins.

    systemd applies a unit's ``.d/*.conf`` drop-ins in lexical order, later files
    overriding earlier keys. ``_load_dropins`` passes them pre-sorted; extracting
    the merge keeps that last-wins behaviour a pure, injectable detail so the
    real-tree caller stays a thin reader.
    """
    merged: dict[str, str] = {}
    for text in conf_texts:
        merged.update(_parse_dropin_conf(text).get("Service", {}))
    return merged


def _load_dropins() -> dict[str, dict[str, str]]:
    """Map each bounded systemd unit to its parsed ``[Service]`` drop-in settings.

    Reads ``deploy/systemd/<unit>.d/*.conf`` for every manifest workload with
    ``kind == "systemd"`` and a class other than ``incidental``, returning
    ``{unit: {"CPUWeight": ..., "MemoryMax": ...}}`` from the merged ``[Service]``
    sections (only the keys present). A unit whose drop-in directory is absent
    yields no entry, so ``_missing_dropin_report`` reds it; the vacuity guard
    reds rather than passes should the whole tree vanish.
    """
    manifest = _load_manifest()
    dropins: dict[str, dict[str, str]] = {}
    for workload in _bounded_systemd_workloads(manifest):
        unit = _manifest_unit_name(workload)
        unit_dir = _DROPINS_DIR / f"{unit}.d"
        if not unit_dir.is_dir():
            continue
        dropins[unit] = _merge_service_settings(
            conf.read_text(encoding="utf-8") for conf in sorted(unit_dir.glob("*.conf"))
        )
    assert dropins, (
        f"No systemd drop-ins resolved under {_DROPINS_DIR}; expected one per "
        "bounded non-incidental systemd workload in "
        f"deploy/{_MANIFEST_PATH.name}. The directory is missing, or the "
        "manifest declares no such workload."
    )
    return dropins


def _present_dropin_units() -> set[str]:
    """Return the unit name of every ``deploy/systemd/<unit>.d`` directory.

    Only directories ending in ``.d`` count, so the sibling ``README.md`` and any
    stray file are ignored; the ``.d`` suffix is stripped so each maps to a unit
    name comparable with ``_manifest_unit_name``. Feeds ``_orphan_dropin_report``.
    """
    return {
        path.name[: -len(".d")] for path in _DROPINS_DIR.glob("*.d") if path.is_dir()
    }


def _load_compose() -> dict[str, Any]:
    """Parse ``docker-compose.yml`` and return its top-level mapping."""
    data = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), data
    return data


def _container_limits_drift_report(
    manifest: dict[str, Any], compose: dict[str, Any]
) -> str:
    """Name each bounded container workload whose limits disagree with compose.

    A workload is resolved to its compose service by ``container_name``; one the
    compose file does not declare is itself drift, so a rename cannot leave the
    manifest bound to nothing.
    """
    services = {
        service.get("container_name"): service
        for service in compose.get("services", {}).values()
    }
    problems: list[str] = []
    for workload in manifest.get("workload", []):
        if workload.get("kind") != "container":
            continue
        if "cpu_limit" not in workload and "memory_limit" not in workload:
            continue
        name = workload["name"]
        service = services.get(name)
        if service is None:
            problems.append(f"{name}: no compose service with that container_name")
            continue
        limits = service.get("deploy", {}).get("resources", {}).get("limits", {})
        for key, compose_key in (("cpu_limit", "cpus"), ("memory_limit", "memory")):
            if workload.get(key) != limits.get(compose_key):
                problems.append(
                    f"{name}: manifest {key}={workload.get(key)!r} != "
                    f"compose {compose_key}={limits.get(compose_key)!r}"
                )
    return "\n".join(problems)


def test_container_limits_match_compose() -> None:
    """Manifest container bounds mirror ``docker-compose.yml`` exactly."""
    report = _container_limits_drift_report(_load_manifest(), _load_compose())
    assert report == "", report


def test_manifest_declares_a_known_vocabulary() -> None:
    """Every declared workload has a known class and a declared tier."""
    report = _vocabulary_report(_load_manifest())
    assert report == "", report


def test_every_workflow_runner_is_declared() -> None:
    """Every ``runs-on:`` across every workflow resolves to a declared label."""
    report = _undeclared_runner_report(_load_manifest(), _load_workflows())
    assert report == "", report


def test_no_undeclared_batch_colocation() -> None:
    """No tier mixes batch with interactive or steady-autonomous work silently."""
    report = _colocation_report(_load_manifest())
    assert report == "", report


def test_no_ci_job_on_a_shared_tier() -> None:
    """No CI job may target a runner on the production host.

    The real-tree assertion behind the flip-back guard: every committed
    ``runs-on:`` must resolve to a tier with no interactive or steady-autonomous
    tenant, unless ``[ci_placement] production_host_jobs`` licenses it.
    """
    report = _misplaced_ci_report(_load_manifest(), _load_workflows())
    assert report == "", report


def test_at_most_one_host_colocated_runner() -> None:
    """Exactly one self-hosted runner may co-tenant the production host.

    The real-tree assertion behind the additive-sprawl guard: the
    ``obsidian`` / ``obsidian-2`` pair collapsed to one deploy runner, so a
    second production-host ``bursty-batch`` runner would red the build naming
    the manifest.
    """
    report = _redundant_host_runner_report(_load_manifest())
    assert report == "", report


def test_every_bounded_systemd_workload_has_a_dropin() -> None:
    """Every non-``incidental`` systemd workload carries a bounded drop-in.

    The real-tree assertion behind the resource-isolation fix: a declared
    systemd host unit that a CI burst can starve must ship an explicit
    ``CPUWeight`` and a finite ``MemoryMax`` under ``deploy/systemd/``.
    """
    report = _missing_dropin_report(_load_manifest(), _load_dropins())
    assert report == "", report


def test_interactive_outranks_batch() -> None:
    """Interactive systemd units are weighted strictly above the batch runners."""
    report = _batch_outranks_interactive_report(_load_manifest(), _load_dropins())
    assert report == "", report


def test_no_orphan_dropin_directory() -> None:
    """Every checked-in drop-in directory maps to a bounded manifest workload.

    The inverse of ``test_every_bounded_systemd_workload_has_a_dropin``: a
    ``deploy/systemd/<unit>.d`` directory left behind by a renamed or deleted
    manifest entry is dead config, which the forward guard cannot see.
    """
    report = _orphan_dropin_report(_load_manifest(), _present_dropin_units())
    assert report == "", report


def test_sweep_covers_the_known_workflows() -> None:
    """The sweep must still reach every workflow committed today.

    ``_committed_workflows`` already fails loudly on an empty glob, so this
    guards the remaining rot: a workflow dropping out of the scanned set while
    its siblings keep the glob non-empty.
    """
    scanned = {path.name for path in _committed_workflows()}
    missing = _KNOWN_WORKFLOWS - scanned
    assert not missing, (
        f"Known committed workflows are missing from the sweep: {sorted(missing)}. "
        "Either the workflow was removed (drop it from _KNOWN_WORKFLOWS) or the "
        "glob no longer resolves .github/workflows."
    )


def test_every_known_workflow_declares_a_runner() -> None:
    """Each committed workflow contributes at least one ``runs-on`` label.

    Without this, a workflow whose ``jobs`` mapping stopped parsing into job
    mappings — or whose jobs all lost their ``runs-on`` — would contribute zero
    triples and sail through the resolution check having pinned nothing.
    """
    covered = {name for name, _job, _label in _runs_on_labels(_load_workflows())}
    missing = _KNOWN_WORKFLOWS - covered
    assert not missing, (
        f"These workflows contributed no 'runs-on' declaration: {sorted(missing)}."
    )


# --- Seeded controls --------------------------------------------------------

_TIERS = [{"name": "production-vps"}, {"name": "managed-pool"}]


def _manifest(
    *workloads: dict[str, Any], ci_placement: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a synthetic manifest around ``workloads`` for the controls below.

    ``ci_placement`` is threaded through only when a control needs it, so the
    existing callers stay unchanged and ``_misplaced_ci_report`` exercises its
    ``manifest.get("ci_placement", {})`` fallback on the manifests that omit it.
    """
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "tier": _TIERS,
        "workload": list(workloads),
    }
    if ci_placement is not None:
        manifest["ci_placement"] = ci_placement
    return manifest


_SHELL = {"name": "shell", "class": "interactive", "tier": "production-vps"}
_LOOP = {"name": "loop", "class": "steady-autonomous", "tier": "production-vps"}
_MANAGED_CI = {
    "name": "managed-ci",
    "class": "bursty-batch",
    "tier": "managed-pool",
    "runner_label": "managed-label",
}
#: A self-hosted runner co-tenanted with the shell/loop on the production tier —
#: the placement the deploy job legitimately uses and every other job must not.
_SELF_HOSTED_RUNNER = {
    "name": "runner",
    "class": "bursty-batch",
    "tier": "production-vps",
    "runner_label": "self-hosted",
    "colocation_exception": {"reason": "Serves deploy.yml; needs the local daemon."},
}


def test_vocabulary_report_fires_on_an_unknown_class() -> None:
    """Negative control: an invented class is named in the report."""
    report = _vocabulary_report(
        _manifest({"name": "odd", "class": "sporadic", "tier": "production-vps"})
    )
    assert "odd" in report
    assert "'sporadic'" in report


def test_vocabulary_report_fires_on_an_undeclared_tier() -> None:
    """Negative control: a tier no ``[[tier]]`` declares is named in the report."""
    report = _vocabulary_report(
        _manifest({"name": "odd", "class": "incidental", "tier": "ghost-tier"})
    )
    assert "odd" in report
    assert "'ghost-tier'" in report


def test_vocabulary_report_is_empty_for_a_well_formed_manifest() -> None:
    """Positive control: a well-formed manifest yields no report."""
    assert _vocabulary_report(_manifest(_SHELL, _MANAGED_CI)) == ""


def test_undeclared_runner_report_fires_on_an_unknown_label() -> None:
    """Negative control: pointing a job back at the production host reds.

    This is the regression the guard exists for — a workflow edited from the
    managed label back to ``self-hosted`` when no manifest workload claims it.
    """
    workflows = {"quality-gates.yml": {"jobs": {"quality": {"runs-on": "self-hosted"}}}}
    report = _undeclared_runner_report(_manifest(_MANAGED_CI), workflows)
    assert "quality-gates.yml:quality" in report
    assert "'self-hosted'" in report
    assert "placement.toml" in report


def test_undeclared_runner_report_accepts_a_declared_label() -> None:
    """Positive control: a job targeting a declared label yields no report."""
    workflows = {"quality-gates.yml": {"jobs": {"quality": {"runs-on": "managed-label"}}}}
    assert _undeclared_runner_report(_manifest(_MANAGED_CI), workflows) == ""


def test_undeclared_runner_report_resolves_every_label_in_a_list() -> None:
    """A list-shaped ``runs-on`` is resolved label by label, not as a whole."""
    workflows = {
        "quality-gates.yml": {
            "jobs": {"quality": {"runs-on": ["managed-label", "gpu"]}}
        }
    }
    report = _undeclared_runner_report(_manifest(_MANAGED_CI), workflows)
    # Anchor on the offender clause, not the whole report: its trailer lists the
    # declared labels, so a bare substring check for 'managed-label' would match
    # there and pass whether or not the label was correctly resolved.
    assert "targets 'gpu'" in report
    assert "targets 'managed-label'" not in report


def test_runs_on_labels_skips_a_reusable_workflow_call() -> None:
    """A job delegating to a reusable workflow declares no runner of its own."""
    workflows = {"call.yml": {"jobs": {"delegated": {"uses": "./.github/workflows/x.yml"}}}}
    assert _runs_on_labels(workflows) == []


def test_runs_on_labels_rejects_an_unsupported_shape() -> None:
    """Negative control: a runner-group mapping fails loudly, never silently."""
    workflows = {
        "quality-gates.yml": {
            "jobs": {"quality": {"runs-on": {"group": "ubuntu", "labels": ["x"]}}}
        }
    }
    with pytest.raises(AssertionError, match="unsupported 'runs-on' shape"):
        _runs_on_labels(workflows)


def test_colocation_report_fires_on_a_bursty_interactive_collision() -> None:
    """Negative control: batch beside the operator's shell without an exception."""
    runner = {"name": "runner", "class": "bursty-batch", "tier": "production-vps"}
    report = _colocation_report(_manifest(_SHELL, runner))
    assert "runner" in report
    assert "shell" in report


def test_colocation_report_fires_on_a_bursty_steady_autonomous_collision() -> None:
    """Negative control: batch beside the loop container without an exception.

    Named separately from the interactive arm because an invariant covering only
    ``interactive`` would pass this manifest while CI starves the two Maestro
    containers.
    """
    runner = {"name": "runner", "class": "bursty-batch", "tier": "production-vps"}
    report = _colocation_report(_manifest(_LOOP, runner))
    assert "runner" in report
    assert "loop" in report


def test_colocation_report_fires_on_an_empty_exception_reason() -> None:
    """Negative control: an exception that says nothing licenses nothing."""
    runner = {
        "name": "runner",
        "class": "bursty-batch",
        "tier": "production-vps",
        "colocation_exception": {"reason": "   "},
    }
    assert "runner" in _colocation_report(_manifest(_SHELL, runner))


def test_colocation_report_accepts_a_written_exception() -> None:
    """Positive control: a written reason licenses the co-tenancy."""
    runner = {
        "name": "runner",
        "class": "bursty-batch",
        "tier": "production-vps",
        "colocation_exception": {"reason": "Serves deploy.yml; needs the local daemon."},
    }
    assert _colocation_report(_manifest(_SHELL, runner)) == ""


def test_colocation_report_ignores_a_retired_workload() -> None:
    """A de-registered tree still on disk is not a live co-tenant."""
    runner = {
        "name": "runner",
        "class": "bursty-batch",
        "tier": "production-vps",
        "status": "retired",
    }
    assert _colocation_report(_manifest(_SHELL, runner)) == ""


def test_colocation_report_ignores_a_retired_starvable_neighbour() -> None:
    """A de-registered interactive/steady workload is not a victim to protect.

    Symmetric with the retired-batch case above: the co-tenancy rule reds only
    when a *live* starvable tenant shares a tier with an active batch one. A
    retired shell still consuming disk is not being starved, so it must not
    force an exception onto an otherwise-clean active batch neighbour.
    """
    retired_shell = {
        "name": "old-shell",
        "class": "interactive",
        "tier": "production-vps",
        "status": "retired",
    }
    runner = {"name": "runner", "class": "bursty-batch", "tier": "production-vps"}
    assert _colocation_report(_manifest(retired_shell, runner)) == ""


def test_colocation_report_ignores_a_batch_only_tier() -> None:
    """A tier holding no starvable class needs no exception at all."""
    assert _colocation_report(_manifest(_MANAGED_CI)) == ""


def test_colocation_report_ignores_incidental_neighbours() -> None:
    """``incidental`` services are neither starved nor starving."""
    hobby = {"name": "hobby", "class": "incidental", "tier": "production-vps"}
    runner = {"name": "runner", "class": "bursty-batch", "tier": "production-vps"}
    assert _colocation_report(_manifest(hobby, runner)) == ""


def test_redundant_host_runner_report_fires_on_a_second_host_runner() -> None:
    """Negative control: a second active runner beside the shell reds the report.

    The additive-sprawl regression the consolidation ends — a second
    production-host runner re-registered alongside the operator's shell.
    """
    runner_a = {"name": "runner-a", "class": "bursty-batch", "tier": "production-vps"}
    runner_b = {"name": "runner-b", "class": "bursty-batch", "tier": "production-vps"}
    report = _redundant_host_runner_report(_manifest(_SHELL, runner_a, runner_b))
    assert "runner-a" in report
    assert "runner-b" in report


def test_redundant_host_runner_report_accepts_a_single_host_runner() -> None:
    """Positive control: exactly one runner beside the shell is clean."""
    assert _redundant_host_runner_report(_manifest(_SHELL, _SELF_HOSTED_RUNNER)) == ""


def test_redundant_host_runner_report_ignores_a_batch_only_tier() -> None:
    """A tier with no latency-sensitive neighbour may hold many batch workloads.

    The managed pool holds only ``bursty-batch`` work, so a second runner there
    starves nothing and must never red — mirroring
    ``test_colocation_report_ignores_a_batch_only_tier``.
    """
    second_managed = {
        "name": "managed-ci-2",
        "class": "bursty-batch",
        "tier": "managed-pool",
        "runner_label": "managed-label",
    }
    assert _redundant_host_runner_report(_manifest(_MANAGED_CI, second_managed)) == ""


def test_redundant_host_runner_report_ignores_a_retired_second_runner() -> None:
    """A de-registered second runner still on disk is not a live co-tenant.

    Symmetric with ``_colocation_report``'s ``active``-only filter: a
    ``retired`` tree consuming disk does not count toward the one-runner bound,
    so an otherwise-clean single active runner stays green.
    """
    retired_runner = {
        "name": "runner-old",
        "class": "bursty-batch",
        "tier": "production-vps",
        "runner_label": "self-hosted",
        "status": "retired",
    }
    report = _redundant_host_runner_report(
        _manifest(_SHELL, _SELF_HOSTED_RUNNER, retired_runner)
    )
    assert report == ""


def test_exception_reason_rejects_malformed_shapes() -> None:
    """Every malformed exception shape collapses to no reason at all."""
    assert _exception_reason({"name": "w"}) == ""
    assert _exception_reason({"colocation_exception": "because"}) == ""
    assert _exception_reason({"colocation_exception": {}}) == ""
    assert _exception_reason({"colocation_exception": {"reason": 7}}) == ""
    assert _exception_reason({"colocation_exception": {"reason": ""}}) == ""
    assert _exception_reason({"colocation_exception": {"reason": "\t\n "}}) == ""


def test_exception_reason_returns_a_written_reason() -> None:
    """A written reason is returned stripped of surrounding whitespace."""
    workload = {"colocation_exception": {"reason": "  Serves deploy.yml.  "}}
    assert _exception_reason(workload) == "Serves deploy.yml."


def test_misplaced_ci_report_fires_on_a_flip_back_to_self_hosted() -> None:
    """Negative control: the regression the flip-back guard exists for.

    ``self-hosted`` is a *declared* runner label, so ``_undeclared_runner_report``
    stays green when a job is pointed back at it. This guard reds instead,
    because the label resolves to a tier co-tenanted with the shell.
    """
    workflows = {"quality-gates.yml": {"jobs": {"quality": {"runs-on": "self-hosted"}}}}
    report = _misplaced_ci_report(_manifest(_SHELL, _SELF_HOSTED_RUNNER), workflows)
    assert "quality-gates.yml:quality" in report
    assert "'self-hosted'" in report
    assert "'production-vps'" in report


def test_misplaced_ci_report_excepts_the_deploy_job() -> None:
    """Positive control: the one licensed placement, allow-listed by the manifest."""
    workflows = {"deploy.yml": {"jobs": {"deploy": {"runs-on": "self-hosted"}}}}
    manifest = _manifest(
        _SHELL,
        _SELF_HOSTED_RUNNER,
        ci_placement={"production_host_jobs": ["deploy.yml:deploy"]},
    )
    assert _misplaced_ci_report(manifest, workflows) == ""


def test_misplaced_ci_report_ignores_a_managed_pool_job() -> None:
    """A job on a tier with no starvable neighbour never reds."""
    workflows = {"quality-gates.yml": {"jobs": {"quality": {"runs-on": "managed-label"}}}}
    assert _misplaced_ci_report(_manifest(_SHELL, _MANAGED_CI), workflows) == ""


def test_misplaced_ci_report_skips_an_undeclared_label() -> None:
    """An undeclared label is left for ``_undeclared_runner_report`` to name."""
    workflows = {"x.yml": {"jobs": {"j": {"runs-on": "mystery"}}}}
    assert _misplaced_ci_report(_manifest(_SHELL, _MANAGED_CI), workflows) == ""


def test_misplaced_ci_report_ignores_a_retired_starvable_tier() -> None:
    """A production tier whose only starvable tenant is retired starves nobody.

    Symmetric with ``_colocation_report``'s retired-tenant handling: a CI job may
    run on such a tier without a placement exception, because there is no live
    latency-sensitive neighbour to protect.
    """
    retired_shell = {
        "name": "old-shell",
        "class": "interactive",
        "tier": "production-vps",
        "status": "retired",
    }
    workflows = {"quality-gates.yml": {"jobs": {"quality": {"runs-on": "self-hosted"}}}}
    report = _misplaced_ci_report(
        _manifest(retired_shell, _SELF_HOSTED_RUNNER), workflows
    )
    assert report == ""


# --- Seeded controls: systemd drop-in bounds --------------------------------

#: A bounded interactive host unit whose ``name`` already carries ``.service``.
_SYSTEMD_SHELL = {
    "name": "devshell.service",
    "class": "interactive",
    "tier": "production-vps",
    "kind": "systemd",
}
#: A bounded batch host unit whose ``name`` lacks ``.service`` (the runner shape),
#: so ``_manifest_unit_name`` resolves it to ``runner.service``.
_SYSTEMD_RUNNER = {
    "name": "runner",
    "class": "bursty-batch",
    "tier": "production-vps",
    "kind": "systemd",
}
#: An ``incidental`` systemd unit — exempt from the drop-in requirement.
_INCIDENTAL_UNIT = {
    "name": "hobby.service",
    "class": "incidental",
    "tier": "production-vps",
    "kind": "systemd",
}
#: A container workload — no host unit to bound.
_CONTAINER_WORKLOAD = {
    "name": "loop",
    "class": "steady-autonomous",
    "tier": "production-vps",
    "kind": "container",
}
#: A managed workload (someone else's machine) — no host unit to bound.
_MANAGED_WORKLOAD = {
    "name": "blacksmith-ci-pool",
    "class": "bursty-batch",
    "tier": "managed-pool",
    "kind": "managed",
}
#: A ``retired`` systemd unit — a de-registered tree still on disk. Per the
#: manifest's status semantics it owns no live host unit, so — like the
#: retired entries the co-location detectors already skip — it neither needs a
#: drop-in nor legitimises a lingering one.
_RETIRED_SYSTEMD_UNIT = {
    "name": "old-runner",
    "class": "bursty-batch",
    "tier": "production-vps",
    "kind": "systemd",
    "status": "retired",
}


def test_parse_dropin_conf_reads_service_settings() -> None:
    """The hand-rolled reader extracts ``[Service]`` keys and skips comments."""
    text = "# placement\n[Service]\nCPUWeight=400\nMemoryMax=6G\n"
    sections = _parse_dropin_conf(text)
    assert sections["Service"]["CPUWeight"] == "400"
    assert sections["Service"]["MemoryMax"] == "6G"


def test_missing_dropin_report_fires_on_a_workload_without_a_dropin() -> None:
    """Negative control: a bounded systemd unit with no drop-in reds."""
    report = _missing_dropin_report(_manifest(_SYSTEMD_SHELL), {})
    assert "devshell.service" in report
    assert "no drop-in" in report


def test_missing_dropin_report_fires_on_infinity_memory_max() -> None:
    """Negative control: an ``infinity`` ceiling is not a finite bound."""
    dropins = {"devshell.service": {"CPUWeight": "400", "MemoryMax": "infinity"}}
    report = _missing_dropin_report(_manifest(_SYSTEMD_SHELL), dropins)
    assert "devshell.service" in report
    assert "MemoryMax" in report


def test_missing_dropin_report_fires_on_a_blank_cpu_weight() -> None:
    """Negative control: a blank ``CPUWeight`` is not an explicit weight."""
    dropins = {"devshell.service": {"CPUWeight": "  ", "MemoryMax": "6G"}}
    report = _missing_dropin_report(_manifest(_SYSTEMD_SHELL), dropins)
    assert "devshell.service" in report
    assert "CPUWeight" in report


def test_missing_dropin_report_fires_on_an_absent_cpu_weight() -> None:
    """Negative control: a drop-in missing ``CPUWeight`` entirely reds."""
    dropins = {"devshell.service": {"MemoryMax": "6G"}}
    report = _missing_dropin_report(_manifest(_SYSTEMD_SHELL), dropins)
    assert "devshell.service" in report
    assert "CPUWeight" in report


def test_missing_dropin_report_exempts_an_incidental_workload() -> None:
    """Positive control: an ``incidental`` systemd unit needs no drop-in."""
    assert _missing_dropin_report(_manifest(_INCIDENTAL_UNIT), {}) == ""


def test_missing_dropin_report_exempts_container_and_managed_workloads() -> None:
    """Positive control: workloads with no host unit need no drop-in."""
    manifest = _manifest(_CONTAINER_WORKLOAD, _MANAGED_WORKLOAD)
    assert _missing_dropin_report(manifest, {}) == ""


def test_missing_dropin_report_accepts_a_well_formed_pair() -> None:
    """Positive control: explicit weights and finite ceilings yield no report."""
    manifest = _manifest(_SYSTEMD_SHELL, _SYSTEMD_RUNNER)
    dropins = {
        "devshell.service": {"CPUWeight": "400", "MemoryMax": "6G"},
        "runner.service": {"CPUWeight": "100", "MemoryMax": "2G"},
    }
    assert _missing_dropin_report(manifest, dropins) == ""


def test_missing_dropin_report_ignores_a_retired_workload() -> None:
    """A de-registered systemd tree owns no live unit, so it needs no drop-in.

    Mirrors the retired-entry skip ``_colocation_report`` and
    ``_misplaced_ci_report`` already apply: demanding a bound for a unit that no
    longer runs would red the build on an impossible drop-in.
    """
    assert _missing_dropin_report(_manifest(_RETIRED_SYSTEMD_UNIT), {}) == ""


def test_batch_outranks_interactive_report_fires_when_batch_ties_interactive() -> None:
    """Negative control: an interactive weight not exceeding a batch one reds."""
    manifest = _manifest(_SYSTEMD_SHELL, _SYSTEMD_RUNNER)
    dropins = {
        "devshell.service": {"CPUWeight": "100", "MemoryMax": "6G"},
        "runner.service": {"CPUWeight": "100", "MemoryMax": "2G"},
    }
    report = _batch_outranks_interactive_report(manifest, dropins)
    assert "does not exceed" in report


def test_batch_outranks_interactive_report_accepts_interactive_above_batch() -> None:
    """Positive control: interactive strictly above batch yields no report."""
    manifest = _manifest(_SYSTEMD_SHELL, _SYSTEMD_RUNNER)
    dropins = {
        "devshell.service": {"CPUWeight": "400", "MemoryMax": "6G"},
        "runner.service": {"CPUWeight": "100", "MemoryMax": "2G"},
    }
    assert _batch_outranks_interactive_report(manifest, dropins) == ""


def test_batch_outranks_interactive_report_is_empty_without_both_tiers() -> None:
    """A manifest with no interactive/batch drop-in pair has nothing to rank."""
    manifest = _manifest(_SYSTEMD_RUNNER)
    dropins = {"runner.service": {"CPUWeight": "100", "MemoryMax": "2G"}}
    assert _batch_outranks_interactive_report(manifest, dropins) == ""


# --- Seeded controls: pure drop-in helpers ----------------------------------
#
# tests/ is not coverage-measured, so an unexercised arm of these helpers is
# flagged by nothing else; each arm named in the helper docstrings gets a control.


def test_cpu_weight_value_collapses_every_malformed_shape() -> None:
    """A missing, boolean, non-positive, or non-numeric weight collapses to None."""
    assert _cpu_weight_value(None) is None
    assert _cpu_weight_value(True) is None  # a bool is not an int weight
    assert _cpu_weight_value(0) is None  # non-positive int
    assert _cpu_weight_value(-5) is None
    assert _cpu_weight_value("0") is None  # non-positive digit string
    assert _cpu_weight_value("") is None
    assert _cpu_weight_value("   ") is None
    assert _cpu_weight_value("high") is None


def test_cpu_weight_value_accepts_a_positive_int_and_digit_string() -> None:
    """A positive int, or a whitespace-padded digit string, resolves to that int."""
    assert _cpu_weight_value(250) == 250
    assert _cpu_weight_value("  400 ") == 400


def test_cpu_weight_value_rejects_non_ascii_digits() -> None:
    """Non-ASCII digits collapse to None instead of crashing or silently parsing.

    ``"²".isdigit()`` is True but ``int("²")`` raises ``ValueError``; Arabic-Indic
    ``"٤٠٠"`` would ``int()`` to ``400`` though systemd rejects it. The ASCII gate
    turns both into ``None``, keeping the collapse-to-None contract crash-free.
    """
    assert _cpu_weight_value("²") is None  # superscript two
    assert _cpu_weight_value("٤٠٠") is None  # Arabic-Indic 400


def test_is_finite_memory_max_across_shapes() -> None:
    """None, blank, ``infinity``, and malformed sizes are not finite ceilings.

    A real systemd size — a plain byte count, an integer/decimal with a binary
    ``K``/``M``/``G``/``T``/``P``/``E`` suffix, or a percentage — is finite; a
    typo systemd would reject (``2GG``, ``banana``) or a non-ASCII digit
    collapses to ``False`` too, so the guard is as strict as ``_cpu_weight_value``
    rather than admitting any non-``infinity`` string.
    """
    assert _is_finite_memory_max(None) is False
    assert _is_finite_memory_max("") is False
    assert _is_finite_memory_max("   ") is False
    assert _is_finite_memory_max("infinity") is False
    assert _is_finite_memory_max("Infinity") is False
    assert _is_finite_memory_max("2GG") is False  # double suffix — systemd rejects
    assert _is_finite_memory_max("banana") is False
    assert _is_finite_memory_max("G") is False  # suffix with no number
    assert _is_finite_memory_max("٤G") is False  # non-ASCII digit
    assert _is_finite_memory_max("6G") is True
    assert _is_finite_memory_max("2048") is True  # a plain byte count
    assert _is_finite_memory_max("1.5G") is True
    assert _is_finite_memory_max("50%") is True


def test_parse_dropin_conf_skips_semicolon_comments() -> None:
    """A ``;`` line — before or inside a section — is a comment, not a setting."""
    sections = _parse_dropin_conf("; lead\n[Service]\n; mid\nCPUWeight=400\n")
    assert sections["Service"] == {"CPUWeight": "400"}


def test_parse_dropin_conf_rejects_a_key_before_any_section() -> None:
    """A ``key=value`` before any ``[Section]`` header fails loudly."""
    with pytest.raises(AssertionError, match=r"before any \[Section\]"):
        _parse_dropin_conf("CPUWeight=400\n")


def test_parse_dropin_conf_rejects_a_line_without_equals() -> None:
    """A non-comment, non-header line with no ``=`` fails loudly."""
    with pytest.raises(AssertionError, match="no '='"):
        _parse_dropin_conf("[Service]\nCPUWeight 400\n")


def test_merge_service_settings_later_conf_wins() -> None:
    """Later drop-in confs override earlier keys, mirroring systemd's last-wins."""
    first = "[Service]\nCPUWeight=100\nMemoryMax=2G\n"
    second = "[Service]\nCPUWeight=200\n"
    merged = _merge_service_settings([first, second])
    assert merged["CPUWeight"] == "200"
    assert merged["MemoryMax"] == "2G"


def test_load_dropins_reds_when_no_bounded_workload_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vacuity guard reds rather than passing on an empty drop-in map.

    A manifest with only a container workload has no bounded systemd unit, so the
    real-tree loader resolves nothing; the assert must fire instead of returning
    an empty map that every drop-in report would then vacuously pass.
    """
    monkeypatch.setattr(
        sys.modules[__name__],
        "_load_manifest",
        lambda: _manifest(_CONTAINER_WORKLOAD),
    )
    with pytest.raises(AssertionError, match="No systemd drop-ins resolved"):
        _load_dropins()


# --- Seeded controls: orphan drop-in directories ----------------------------


def test_orphan_dropin_report_fires_on_an_unclaimed_directory() -> None:
    """Negative control: a drop-in dir no manifest workload claims is named."""
    report = _orphan_dropin_report(
        _manifest(_SYSTEMD_SHELL), {"devshell.service", "ghost.service"}
    )
    assert "ghost.service.d" in report
    assert "devshell.service.d" not in report


def test_orphan_dropin_report_flags_a_directory_for_an_incidental_unit() -> None:
    """An ``incidental`` unit owns no drop-in, so a directory for it is an orphan."""
    report = _orphan_dropin_report(_manifest(_INCIDENTAL_UNIT), {"hobby.service"})
    assert "hobby.service.d" in report


def test_orphan_dropin_report_accepts_a_fully_claimed_tree() -> None:
    """Positive control: every present dir maps to a bounded workload — no report."""
    manifest = _manifest(_SYSTEMD_SHELL, _SYSTEMD_RUNNER)
    assert (
        _orphan_dropin_report(manifest, {"devshell.service", "runner.service"}) == ""
    )


def test_orphan_dropin_report_flags_a_retired_units_dropin() -> None:
    """A drop-in for a de-registered unit is dead config, not a live bound.

    The inverse of ``test_missing_dropin_report_ignores_a_retired_workload``:
    once a systemd entry is ``retired`` its lingering
    ``deploy/systemd/<unit>.d`` directory is an orphan to remove, so the retired
    entry is excluded from the bounded set both ways — no drop-in demanded, and
    any lingering one named as dead config.
    """
    report = _orphan_dropin_report(
        _manifest(_RETIRED_SYSTEMD_UNIT), {"old-runner.service"}
    )
    assert "old-runner.service.d" in report


def _compose(**limits_by_container: dict[str, str]) -> dict[str, Any]:
    """Build a synthetic compose mapping for the container-limit controls."""
    return {
        "services": {
            name: {
                "container_name": name,
                "deploy": {"resources": {"limits": limits}},
            }
            for name, limits in limits_by_container.items()
        }
    }


_BOUNDED_CONTAINER = {
    "name": "loop",
    "class": "steady-autonomous",
    "tier": "production-vps",
    "kind": "container",
    "cpu_limit": "4.0",
    "memory_limit": "6G",
}


def test_container_limits_agree_control() -> None:
    compose = _compose(loop={"cpus": "4.0", "memory": "6G"})
    report = _container_limits_drift_report(_manifest(_BOUNDED_CONTAINER), compose)
    assert report == ""


def test_container_limits_mismatch_is_reported() -> None:
    compose = _compose(loop={"cpus": "2.0", "memory": "6G"})
    report = _container_limits_drift_report(_manifest(_BOUNDED_CONTAINER), compose)
    assert "loop: manifest cpu_limit='4.0' != compose cpus='2.0'" in report
    assert "memory_limit" not in report


def test_container_limits_missing_service_is_reported() -> None:
    report = _container_limits_drift_report(_manifest(_BOUNDED_CONTAINER), _compose())
    assert report == "loop: no compose service with that container_name"


def test_unbounded_and_non_container_workloads_are_skipped() -> None:
    unbounded = {k: v for k, v in _BOUNDED_CONTAINER.items() if "limit" not in k}
    systemd = {**_BOUNDED_CONTAINER, "name": "unit", "kind": "systemd"}
    report = _container_limits_drift_report(_manifest(unbounded, systemd), _compose())
    assert report == ""
