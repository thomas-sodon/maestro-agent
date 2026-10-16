"""Runtime contract — one declared owner of what the container must provide (proposal 141).

Maestro's code depends on facts about the production container — binaries on
PATH, first-party siblings that must resolve as real distributions, writable
paths, cgroup limits, the runtime identity, the shell toolbelt the prompts
assume — that were written down nowhere executable. ``deploy/runtime_contract.toml``
declares them; this module owns that declaration.

Three layers, kept apart so the verdict is testable without a container:

* :func:`load_contract` — the module's only disk read; parses and validates the
  TOML, raising :class:`ContractError` naming the offending entry.
* :func:`evaluate` — pure; folds a :class:`RuntimeContract`, a
  :class:`RequirementContext` and injected :class:`Observations` into a
  :class:`ContractVerdict` with one :class:`Finding` per entry.
* :func:`observe` — the one impure reader. It *reuses* the existing readers
  (``shutil.which``, :func:`maestro.gate_environment._resolves`, the
  ``gate_capacity`` cgroup readers, the ``host_capacity`` ``/proc`` and disk
  readers) so no fact gets a second reader.

Read-only throughout: nothing here writes, mutates, or reaches a credential,
SDK or host seam. It is offline unless ``observe(..., network=True)`` is asked
to resolve the required ``launcher_package`` entries against their launcher's
public registry (one ``GET`` each). ``maestro runtime-contract`` is the operator
surface.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pwd
import re
import shutil
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

import httpx

from maestro import gate_capacity, gate_environment, host_capacity
from maestro.config import Config

logger = logging.getLogger(__name__)

#: Resolved relative to the source checkout. ``deploy/`` is ``.dockerignore``d,
#: so this only exists where the repo is present (the compose ``/workspace``
#: bind mount); an image-only run without it gets :class:`ContractError`.
DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "runtime_contract.toml"
)

ContractKind = Literal[
    "binary",
    "distribution",
    "writable_path",
    "readable_fact",
    "identity",
    "toolbelt",
    "launcher_package",
]
FindingStatus = Literal["ok", "violated", "not-required", "host-only", "unverified"]

#: The kind-specific fields each kind must carry.
_KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "binary": (),
    "toolbelt": (),
    "distribution": ("submodule",),
    "writable_path": ("path",),
    "readable_fact": ("reader",),
    "identity": ("uid", "gid", "home"),
    "launcher_package": ("launcher",),
}
_SCOPES = frozenset({"container", "host"})
_MEMORY_DIR_TOKEN = "{memory_dir}"
_HOME_TOKEN = "{home}"

#: ``launcher_package`` launcher → (registry label, metadata base URL). A
#: launcher missing here is rejected at load, never silently ``unverified``.
_PACKAGE_REGISTRIES: dict[str, tuple[str, str]] = {
    "npx": ("npm", "https://registry.npmjs.org"),
    "uvx": ("PyPI", "https://pypi.org/pypi"),
}
_NPM_NAME = re.compile(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
_PYPI_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")
_RESOLVE_TIMEOUT_SECONDS = 10.0


def _package_url(launcher: str, spec: str) -> str | None:
    """The registry metadata URL for *spec* under *launcher*, or ``None``.

    npm specs are ``name`` / ``@scope/name`` / ``name@ver``; PyPI specs are
    ``name`` / ``name==ver``. A pinned version is requested exactly, so a
    version the registry no longer serves fails to resolve; an unpinned npm
    spec requests ``latest`` (what ``npx -y`` resolves), not the full packument.
    """
    if launcher == "npx":
        head, sep, version = spec[1:].rpartition("@")
        name, version = (spec[:1] + head, version) if sep else (spec, "")
        pattern = _NPM_NAME
    elif launcher == "uvx":
        name, sep, version = spec.partition("==")
        pattern = _PYPI_NAME
    else:
        return None
    if not pattern.fullmatch(name) or (sep and not _VERSION.fullmatch(version)):
        return None
    base = _PACKAGE_REGISTRIES[launcher][1]
    if launcher == "npx":
        url = f"{base}/{quote(name, safe='@')}"
        return f"{url}/{version or 'latest'}"
    return f"{base}/{name}/{version}/json" if version else f"{base}/{name}/json"


def _cgroup_cpu_quota() -> float | None:
    cores = gate_capacity._read_cgroup_quota_cores()
    return float(cores) if cores > 0 else None


def _cgroup_memory_headroom() -> float | None:
    reading = gate_capacity._read_cgroup_memory()
    return float(reading.headroom_mib) if reading.enforced else None


def _meminfo() -> float | None:
    return host_capacity._read_memory_and_swap()[0]


def _docker_root_free() -> float | None:
    return host_capacity._read_docker_root_free_gib()[0]


def _load_average() -> float | None:
    return host_capacity._read_load_per_core()


#: ``readable_fact`` reader name → the existing reader it delegates to. A
#: ``None`` reading means the fact could not be obtained (no enforced cgroup
#: quota or memory limit reads as ``None`` too — the limit is not there to read).
_FACT_READERS: dict[str, Callable[[], float | None]] = {
    "cgroup_cpu_quota": _cgroup_cpu_quota,
    "cgroup_memory_limit": _cgroup_memory_headroom,
    "meminfo": _meminfo,
    "docker_root_free": _docker_root_free,
    "load_average": _load_average,
}

#: Display label for a reading whose value is not the fact its reader names —
#: an enforced memory limit is evidenced by its headroom, which may be ``0``.
_FACT_LABELS: dict[str, str] = {"cgroup_memory_limit": "cgroup_memory_headroom_mib"}


class ContractError(ValueError):
    """The runtime contract TOML is malformed; the message names the entry."""


@dataclass(frozen=True)
class ContractEntry:
    """One declared requirement of the runtime environment."""

    kind: ContractKind
    name: str
    required_when: tuple[str, ...]
    provision: str
    scope: Literal["container", "host"] = "container"
    submodule: str = ""
    path: str = ""
    reader: str = ""
    uid: int | None = None
    gid: int | None = None
    home: str = ""
    launcher: str = ""
    #: ``readable_fact`` only — the kernel/host paths its reader touches.
    paths: tuple[str, ...] = ()
    #: ``writable_path`` only — the ``maestro/`` modules that depend on it.
    used_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeContract:
    entries: tuple[ContractEntry, ...]


def _bool_config_fields() -> frozenset[str]:
    return frozenset(
        f.name for f in dataclasses.fields(Config) if f.type in (bool, "bool")
    )


def _validate_token(token: object, label: str) -> str:
    if not isinstance(token, str):
        raise ContractError(f"{label}: required_when token {token!r} is not a string")
    if token == "always":
        return token
    prefix, sep, value = token.partition(":")
    if not sep or not value or prefix not in ("config", "provider", "env"):
        raise ContractError(f"{label}: malformed required_when token {token!r}")
    if prefix == "config" and value not in _bool_config_fields():
        raise ContractError(
            f"{label}: required_when {token!r} names no boolean Config field"
        )
    return token


def _string_list(
    raw: Mapping[str, object], field_name: str, owner_kind: str,
    kind: object, label: str,
) -> tuple[str, ...]:
    """Validate an optional list-of-strings field only *owner_kind* may carry."""
    if field_name not in raw:
        return ()
    if kind != owner_kind:
        raise ContractError(f"{label}: {field_name} is only valid on {owner_kind}")
    value = raw[field_name]
    if not isinstance(value, list):
        raise ContractError(f"{label}: {field_name} {value!r} is not a list")
    for item in value:
        if not isinstance(item, str):
            raise ContractError(f"{label}: {field_name} item {item!r} is not a string")
    return tuple(cast(list[str], value))


def _parse_entry(index: int, raw: Mapping[str, object]) -> ContractEntry:
    name = raw.get("name")
    label = f"entry {index} ({name!r})"
    if not isinstance(name, str) or not name:
        raise ContractError(f"{label}: missing name")
    kind = raw.get("kind")
    if kind not in _KIND_FIELDS:
        raise ContractError(f"{label}: unknown kind {kind!r}")
    scope = raw.get("scope", "container")
    if scope not in _SCOPES:
        raise ContractError(f"{label}: unknown scope {scope!r}")
    missing = [f for f in _KIND_FIELDS[str(kind)] if f not in raw]
    if missing:
        raise ContractError(f"{label}: kind {kind!r} requires {', '.join(missing)}")
    reader = raw.get("reader", "")
    if kind == "readable_fact" and reader not in _FACT_READERS:
        raise ContractError(f"{label}: unknown reader {reader!r}")
    if "required_when" not in raw:
        raise ContractError(f"{label}: missing required_when")
    when = raw.get("required_when")
    tokens = when if isinstance(when, list) else [when]
    if not tokens:
        raise ContractError(f"{label}: empty required_when")
    uid, gid = raw.get("uid"), raw.get("gid")
    for field_name, value in (("uid", uid), ("gid", gid)):
        if field_name in raw and (not isinstance(value, int) or isinstance(value, bool)):
            raise ContractError(f"{label}: {field_name} {value!r} is not an integer")
    if kind == "launcher_package":
        launcher = raw["launcher"]
        if not isinstance(launcher, str) or launcher not in _PACKAGE_REGISTRIES:
            raise ContractError(f"{label}: launcher {launcher!r} has no registry")
        if _package_url(launcher, name) is None:
            raise ContractError(
                f"{label}: unparseable {launcher} package spec {name!r}"
            )
    if "home" in raw and not isinstance(raw["home"], str):
        raise ContractError(f"{label}: home {raw['home']!r} is not a string")
    paths = _string_list(raw, "paths", "readable_fact", kind, label)
    for item in paths:
        if not item.startswith("/"):
            raise ContractError(f"{label}: paths item {item!r} is not absolute")
    used_by = _string_list(raw, "used_by", "writable_path", kind, label)
    for item in used_by:
        if not (item.startswith("maestro/") and item.endswith(".py")):
            raise ContractError(
                f"{label}: used_by item {item!r} is not a maestro/...py path"
            )
    return ContractEntry(
        kind=cast(ContractKind, kind),
        name=name,
        required_when=tuple(_validate_token(t, label) for t in tokens),
        provision=str(raw.get("provision", "")),
        scope=cast(Literal["container", "host"], scope),
        submodule=str(raw.get("submodule", "")),
        path=str(raw.get("path", "")),
        reader=str(reader),
        uid=uid if isinstance(uid, int) else None,
        gid=gid if isinstance(gid, int) else None,
        home=str(raw.get("home", "")),
        launcher=str(raw.get("launcher", "")),
        paths=paths,
        used_by=used_by,
    )


def load_contract(path: Path | None = None) -> RuntimeContract:
    """Load and validate the runtime contract TOML (the module's only disk read)."""
    source = DEFAULT_CONTRACT_PATH if path is None else path
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractError(
            f"runtime contract not found at {source} (run from a source checkout)"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError(f"runtime contract unreadable at {source}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"runtime contract malformed at {source}: {exc}") from exc
    raw_entries = data.get("entry", [])
    if not isinstance(raw_entries, list) or not all(
        isinstance(raw, dict) for raw in raw_entries
    ):
        raise ContractError(f"runtime contract at {source}: entry must be an array of tables")
    return RuntimeContract(
        entries=tuple(_parse_entry(i, raw) for i, raw in enumerate(raw_entries))
    )


@dataclass(frozen=True)
class RequirementContext:
    """What decides whether an entry is required: flags, providers, env."""

    flags: frozenset[str]
    providers: frozenset[str]
    env: str


def requirement_context(config: Config, env_name: str | None) -> RequirementContext:
    """Build the :class:`RequirementContext` from a ``Config`` and ``MAESTRO_ENV``."""
    flags = frozenset(
        name for name in _bool_config_fields() if getattr(config, name) is True
    )
    providers = frozenset(
        {config.resolved_sdk_provider(), *config.sdk_phase_providers.values()}
    )
    return RequirementContext(flags=flags, providers=providers, env=env_name or "")


def requirement_holds(entry: ContractEntry, context: RequirementContext) -> bool:
    """Whether any of *entry*'s ``required_when`` tokens holds under *context*."""
    for token in entry.required_when:
        if token == "always":
            return True
        prefix, _, value = token.partition(":")
        if prefix == "config" and value in context.flags:
            return True
        if prefix == "provider" and value in context.providers:
            return True
        if prefix == "env" and value == context.env:
            return True
    return False


@dataclass(frozen=True)
class PackageResolution:
    """Whether a ``launcher_package`` resolved on its registry, and why."""

    resolved: bool
    detail: str


@dataclass(frozen=True)
class Observations:
    """What :func:`observe` read from the live environment.

    ``writable_paths`` holds the *templates* (``entry.path``) observed writable;
    ``facts`` maps reader name → reading (``None`` when unobtainable);
    ``packages`` maps a ``launcher_package`` name → its registry resolution,
    and is ``None`` when no network check ran.
    """

    found_binaries: frozenset[str]
    resolved_distributions: frozenset[str]
    writable_paths: frozenset[str]
    facts: Mapping[str, float | None]
    identity: tuple[int, int, str]
    packages: Mapping[str, PackageResolution] | None = None


@dataclass(frozen=True)
class Finding:
    entry: ContractEntry
    status: FindingStatus
    detail: str


@dataclass(frozen=True)
class ContractVerdict:
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.status == "violated" for f in self.findings)


def _check(entry: ContractEntry, obs: Observations) -> tuple[bool, str]:
    """Whether a required container-scoped *entry* is satisfied, and why."""
    if entry.kind in ("binary", "toolbelt"):
        found = entry.name in obs.found_binaries
        return found, "on PATH" if found else "not on PATH"
    if entry.kind == "distribution":
        found = entry.name in obs.resolved_distributions
        if found:
            return True, f"{entry.submodule} resolves"
        return False, f"{entry.name}/{entry.submodule} does not resolve"
    if entry.kind == "writable_path":
        writable = entry.path in obs.writable_paths
        return writable, f"{entry.path} {'writable' if writable else 'not writable'}"
    if entry.kind == "readable_fact":
        value = obs.facts.get(entry.reader)
        if value is None:
            return False, f"{entry.reader} unreadable"
        return True, f"{_FACT_LABELS.get(entry.reader, entry.reader)}={value:g}"
    expected = (entry.uid, entry.gid, entry.home)
    uid, gid, home = obs.identity
    detail = f"uid={uid} gid={gid} home={home}"
    return obs.identity == expected, detail


def evaluate(
    contract: RuntimeContract,
    context: RequirementContext,
    observations: Observations,
) -> ContractVerdict:
    """Fold the contract against the observations into one finding per entry. Pure."""
    findings: list[Finding] = []
    for entry in contract.entries:
        if not requirement_holds(entry, context):
            findings.append(Finding(entry, "not-required", ""))
        elif entry.scope == "host":
            findings.append(Finding(entry, "host-only", "verify on the host"))
        elif entry.kind == "launcher_package":
            resolution = (observations.packages or {}).get(entry.name)
            if resolution is None:
                findings.append(Finding(
                    entry, "unverified",
                    f"{entry.launcher} package not resolved (no network check)",
                ))
            elif resolution.resolved:
                findings.append(Finding(entry, "ok", resolution.detail))
            else:
                findings.append(Finding(
                    entry, "violated",
                    f"{resolution.detail}; provision: {entry.provision}",
                ))
        else:
            satisfied, detail = _check(entry, observations)
            if satisfied:
                findings.append(Finding(entry, "ok", detail))
            else:
                findings.append(Finding(
                    entry, "violated", f"{detail}; provision: {entry.provision}",
                ))
    return ContractVerdict(findings=tuple(findings))


def _expand(template: str, config: Config, home: str) -> str:
    return template.replace(_MEMORY_DIR_TOKEN, str(config.memory_dir)).replace(
        _HOME_TOKEN, home,
    )


def _nearest_existing(path: str) -> str:
    probe = path
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return probe
        probe = parent
    return probe


def _identity() -> tuple[int, int, str]:
    uid = os.getuid()
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        home = ""
    return uid, os.getgid(), home


def _resolve_packages(
    entries: tuple[ContractEntry, ...], client: httpx.Client,
) -> dict[str, PackageResolution]:
    """Resolve each ``launcher_package`` entry with one ``GET`` on its registry."""
    resolutions: dict[str, PackageResolution] = {}
    for entry in entries:
        registry = _PACKAGE_REGISTRIES[entry.launcher][0]
        url = cast(str, _package_url(entry.launcher, entry.name))
        try:
            status = client.get(url).status_code
        except httpx.HTTPError as exc:
            resolutions[entry.name] = PackageResolution(
                False, f"{registry} unreachable: {exc}",
            )
            continue
        if status == 200:
            resolution = PackageResolution(True, f"{entry.name} resolves on {registry}")
        elif status == 404:
            resolution = PackageResolution(False, f"{entry.name} not found on {registry}")
        else:
            resolution = PackageResolution(False, f"{registry} answered {status}")
        resolutions[entry.name] = resolution
    return resolutions


def observe(
    contract: RuntimeContract,
    config: Config,
    *,
    network: bool = False,
    context: RequirementContext | None = None,
    client: httpx.Client | None = None,
) -> Observations:
    """Read the live environment for every entry, through the existing readers.

    Non-mutating: a ``writable_path`` is probed with ``os.access(W_OK)`` on the
    path or its nearest existing parent (``access(2)`` reports ``EROFS`` on a
    read-only mount), never by writing.

    Offline unless *network*: then each required, container-scoped
    ``launcher_package`` under *context* (built from *config* and
    ``MAESTRO_ENV`` when omitted) is resolved on its registry through *client*,
    or a short-lived client this call opens and closes.
    """
    binaries: set[str] = set()
    distributions: set[str] = set()
    writable: set[str] = set()
    facts: dict[str, float | None] = {}
    identity = _identity()
    # Mirror ``posixpath.expanduser``: ``Path.home()`` and the claude CLI read
    # ``$HOME`` first, so that is the home the declared users actually write.
    home = os.environ.get("HOME") or identity[2]
    for entry in contract.entries:
        if entry.kind in ("binary", "toolbelt"):
            if shutil.which(entry.name) is not None:
                binaries.add(entry.name)
        elif entry.kind == "distribution":
            if gate_environment._resolves(
                entry.name, find_spec,
            ) and gate_environment._resolves(entry.submodule, find_spec):
                distributions.add(entry.name)
        elif entry.kind == "writable_path":
            if _HOME_TOKEN in entry.path and not home:
                continue  # no home to probe: leave it unobserved, never probe "/"
            target = _nearest_existing(_expand(entry.path, config, home))
            if os.access(target, os.W_OK):
                writable.add(entry.path)
        elif entry.kind == "readable_fact":
            facts[entry.reader] = _FACT_READERS[entry.reader]()
    packages: dict[str, PackageResolution] | None = None
    if network:
        ctx = context or requirement_context(config, os.environ.get("MAESTRO_ENV"))
        wanted = tuple(
            e for e in contract.entries
            if e.kind == "launcher_package" and e.scope == "container"
            and requirement_holds(e, ctx)
        )
        if client is not None:
            packages = _resolve_packages(wanted, client)
        else:
            with httpx.Client(timeout=_RESOLVE_TIMEOUT_SECONDS) as own:
                packages = _resolve_packages(wanted, own)
    return Observations(
        found_binaries=frozenset(binaries),
        resolved_distributions=frozenset(distributions),
        writable_paths=frozenset(writable),
        facts=facts,
        identity=identity,
        packages=packages,
    )


def contract_violations(config: Config, env_name: str | None) -> tuple[str, ...]:
    """One ``"<kind> <name>: <detail>"`` line per violated required entry.

    Offline (``observe(network=False)``), so a ``launcher_package`` stays
    ``unverified`` and never counts. A missing or malformed contract is itself
    one violation line rather than an exception.
    """
    try:
        contract = load_contract()
    except ContractError as exc:
        return (f"runtime contract: {exc}",)
    context = requirement_context(config, env_name)
    observations = observe(contract, config, network=False, context=context)
    verdict = evaluate(contract, context, observations)
    return tuple(
        f"{f.entry.kind} {f.entry.name}: {f.detail}"
        for f in verdict.findings
        if f.status == "violated"
    )


def enforce_runtime_contract(
    config: Config, *, enforced: bool, env_name: str | None,
) -> tuple[str, ...]:
    """Apply ``[loop] runtime_contract_enforced`` to the offline contract probe.

    Enforced with violations: log each at ERROR and raise ``SystemExit`` naming
    every entry. Otherwise log each at WARNING and return the lines.
    """
    violations = contract_violations(config, env_name)
    if enforced and violations:
        for line in violations:
            logger.error("Runtime contract violated: %s", line)
        raise SystemExit("\n".join(violations))
    for line in violations:
        logger.warning("Runtime contract violated: %s", line)
    return violations
