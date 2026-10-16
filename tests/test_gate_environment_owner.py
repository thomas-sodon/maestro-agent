"""AST regression: the gate-runnability vocabulary has exactly one owner.

Proposal 133. "Can the local gate reach a verdict?" is a single fact, and the
whole point of :mod:`maestro.gate_environment` is that it is spelled once. These
guards pin that, in the shape ``tests/test_gate_command_owner.py`` and
``tests/test_gate_lease_owner.py`` already take:

* the six vocabulary symbols (``GateRunnabilityKind``,
  ``GateRunnabilityVerdict``, ``probe_runnability``, ``refresh_verdict``,
  ``read_verdict``, ``format_verdict``) are *defined* only in the owner module —
  a second definition anywhere under ``maestro/`` is a fork of the fact, and
  ``format_verdict`` is protected for the same reason it exists: a CLI command
  that rendered a verdict itself would have to spell a kind;
* the owner imports nothing from :mod:`maestro.gate_capacity` — the one-way edge
  that keeps a future seam binding the lease to this oracle from recursing back
  into the collector (the telemetry row is matched structurally instead);
* no module outside the owner re-spells the runnability-kind set as string
  literals — a consumer must import the alias, not restate its members;
* no module outside the owner calls ``find_spec`` on a first-party sibling name.
  ``probe_provisioning`` is the single spelling of "is the sibling really
  installed?"; ``maestro preflight`` used to carry a rival, narrower one (issue
  #3872), and two spellings of one fact is exactly the drift this owner exists
  to end;
* the lease seam (:mod:`maestro.gate_capacity`) *consults* the oracle and never
  probes it: it may call ``read_verdict``, but calling ``refresh_verdict`` there
  would run the collector, which itself runs through the seam — so the one edge
  the owner's import guard cannot express (the consult runs the other way) is
  pinned from this side;
* every **first-party** top-level package the first-party trees import —
  ``tests/`` *and* ``maestro/``, since ``maestro/config.py`` imports
  ``maestro_sdk`` at function scope and a sibling reached only from the package
  widens the same blind spot — is registered in the owner's
  :data:`~maestro.gate_environment._SIBLINGS`, so a new sibling import cannot
  silently widen the ``UNPROVISIONED`` probe's blind spot. The walk counts
  *function-scope* imports, which is what a collection probe cannot see:
  ``tests/test_config.py`` imports ``maestro_sdk`` only inside test bodies, so
  ``--collect-only`` never reaches it and only a provisioning check catches a
  missing install. ``_SIBLINGS`` is now pinned in three places — the oracle
  itself, this guard, and ``tests/test_packaging_boundary.py``'s
  ``_SIBLING_MODULES`` — and this guard is the runtime-side counterpart of
  ``tests/test_dockerfile_structure.py``'s deployment-side
  ``test_sibling_editable_installs_match_ci``;
* every submodule that registry *declares* resolves on disk — the symmetric
  failure, where a renamed submodule would pin ``probe_provisioning`` to
  ``UNPROVISIONED`` on a perfectly sound environment;
* the approved-plan selector (``loop/implementation.py::_implement_approved_plans``) consults
  the oracle *before* it spends an implementation attempt. The binding is an
  ordering, not a presence: a consult moved below the ``implement_plan`` call,
  or deleted in a refactor, lets an approved plan be spent, demoted,
  cold-restarted and filed as a GitHub issue against a red no diff can clear —
  the exact failure the hold exists to prevent;
* exactly one site *produces* the ``"ENVIRONMENT"`` attribution, and it lives in
  :mod:`maestro.gate_observation` (``attribute``). A second site returning that
  verdict would let a genuine regression be excused as a broken environment.
  Reading it back is not producing: the ``attribution == "ENVIRONMENT"``
  comparisons in ``maestro/evolution.py`` and ``maestro/attempt_disposition.py``
  and the owner's own ``Literal[...]`` alias member stay legal.

Each guard carries an in-memory seeded-fault control per
``tests/test_fault_seeding.py``: the detector is run against synthetic source
carrying the violation it exists to catch, so a guard that has gone silently dead
fails the build rather than passing vacuously.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from maestro import gate_environment as ge
from tests.support.gate_sites import MAESTRO_ROOT, REPO_ROOT
from tests.support.structural_guard import bound_symbol_names, imports_from_prefix

# The owner module, ``maestro``-relative.
_OWNER = "gate_environment.py"

# The first-party trees the sibling-import guard walks. ``maestro/`` is in the
# walk because a sibling imported only from the package — as ``maestro/config.py``
# imports ``maestro_sdk`` at function scope — widens the same blind spot.
_WALKED_ROOTS: tuple[Path, ...] = (REPO_ROOT / "tests", MAESTRO_ROOT)

# The repo-root directories that are never sibling distributions.
_NOT_SIBLINGS: frozenset[str] = frozenset({"maestro", "tests"})

# The vocabulary symbols that may be defined only in the owner.
_OWNED_SYMBOLS: frozenset[str] = frozenset(
    {
        "GateRunnabilityKind",
        "GateRunnabilityVerdict",
        "probe_runnability",
        "refresh_verdict",
        "read_verdict",
        "format_verdict",
    },
)

# The module the owner must not import from, so the edge stays one-way.
_FORBIDDEN_IMPORT = "maestro.gate_capacity"

# The lease seam, ``maestro``-relative: the one consumer that reaches *back* into
# the owner from inside a gate run, so its consult must stay probe-free.
_SEAM = "gate_capacity.py"

# The consult the seam may make, and the probe it may not. ``refresh_verdict``
# runs the collector, and the collector runs through the seam.
_ALLOWED_CONSULT = "read_verdict"
_FORBIDDEN_PROBE = "refresh_verdict"

# The closed kind set. A module outside the owner naming *all* of these as
# string constants is restating the vocabulary rather than importing it.
_KIND_MEMBERS: frozenset[str] = frozenset(
    {"SOUND", "UNPROVISIONED", "UNCOLLECTABLE", "UNFIT"},
)


def _maestro_sources() -> list[Path]:
    """Every ``.py`` file under ``maestro/``."""
    return sorted(MAESTRO_ROOT.rglob("*.py"))


def _defines(source: str, rel: str) -> set[str]:
    """The owned symbols *source* binds at module or class scope."""
    return bound_symbol_names(ast.parse(source, filename=rel)) & _OWNED_SYMBOLS


def _imports_capacity(source: str) -> bool:
    """Whether *source* imports anything from :mod:`maestro.gate_capacity`."""
    return imports_from_prefix(ast.parse(source), _FORBIDDEN_IMPORT, "maestro")


def _string_constants(source: str) -> set[str]:
    """Every string constant in *source*."""
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _restates_the_kind_set(source: str) -> bool:
    """Whether *source* spells every runnability kind as a string literal."""
    return _string_constants(source) >= _KIND_MEMBERS


def _called_names(source: str) -> set[str]:
    """Every name *source* calls, whether bare or through an attribute access.

    ``read_verdict(path)`` and ``gate_environment.read_verdict(path)`` both count,
    so the guard cannot be evaded by changing the import spelling.
    """
    called: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    return called


@pytest.mark.timeout(15)
def test_vocabulary_is_defined_only_in_the_owner() -> None:
    """No module under ``maestro/`` but the owner may define the vocabulary."""
    violators = [
        f"{path.name}:{sorted(defined)}"
        for path in _maestro_sources()
        if path.name != _OWNER and (defined := _defines(path.read_text(encoding="utf-8"), path.name))
    ]

    assert not violators, (
        f"gate-runnability vocabulary defined outside maestro/{_OWNER} (proposal 133):\n  " + "\n  ".join(violators)
    )


@pytest.mark.timeout(15)
def test_the_owner_defines_every_protected_symbol() -> None:
    """The guard is non-vacuous: each protected symbol still exists in the owner."""
    owner = (MAESTRO_ROOT / _OWNER).read_text(encoding="utf-8")

    assert _defines(owner, _OWNER) == _OWNED_SYMBOLS


def test_definition_guard_flags_a_synthetic_second_definition() -> None:
    """A hand-written rival definition is detected (seeded-fault control)."""
    seeded = "def probe_runnability() -> None:\n    return None\n"

    assert _defines(seeded, "<synthetic>") == {"probe_runnability"}
    assert _defines("def unrelated() -> None:\n    return None\n", "<synthetic>") == set()


@pytest.mark.timeout(15)
def test_the_owner_does_not_import_the_lease() -> None:
    """The oracle -> lease edge stays one-way, so no future seam can recurse."""
    owner = (MAESTRO_ROOT / _OWNER).read_text(encoding="utf-8")

    assert not _imports_capacity(owner)


def test_import_guard_flags_a_synthetic_lease_import() -> None:
    """Every import spelling is detected (seeded-fault control)."""
    assert _imports_capacity("from maestro.gate_capacity import GateRunRecord\n")
    assert _imports_capacity("import maestro.gate_capacity\n")
    assert _imports_capacity("from maestro import gate_capacity\n")
    assert _imports_capacity("from . import gate_capacity\n")
    assert not _imports_capacity("from maestro.gate_observation import parse_gate_output\n")


@pytest.mark.timeout(15)
def test_no_module_outside_the_owner_restates_the_kind_set() -> None:
    """A consumer must import ``GateRunnabilityKind``, not re-spell its members."""
    violators = [
        path.name
        for path in _maestro_sources()
        if path.name != _OWNER and _restates_the_kind_set(path.read_text(encoding="utf-8"))
    ]

    assert not violators, (
        "the runnability-kind set is re-spelled as literals outside "
        f"maestro/{_OWNER} (proposal 133): {', '.join(violators)}"
    )


# The provisioning probe's spelling: a call to this name carrying a first-party
# sibling module as its literal argument is the rival check the owner replaced.
_PROVISIONING_CALL = "find_spec"


def _sibling_names() -> frozenset[str]:
    """Every first-party module name the owner's registry declares."""
    return frozenset(name for pair in ge._SIBLINGS for name in pair)


def _called_name(func: ast.expr) -> str | None:
    """The called function's own name, in either spelling, else ``None``.

    A bare ``find_spec(...)`` parses as :class:`ast.Name` and an attribute call
    such as ``importlib.util.find_spec(...)`` as :class:`ast.Attribute`; both
    reduce to the same trailing name, so the guard cannot be evaded by changing
    the import. Any other callable shape (a subscript, a call result) has no
    statically-known name.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _probes_a_sibling(source: str) -> set[str]:
    """The sibling names *source* passes to ``find_spec`` as a string literal.

    Only the first positional argument is read, which is the module name
    ``find_spec`` resolves.
    """
    siblings = _sibling_names()
    probed: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if _called_name(node.func) != _PROVISIONING_CALL:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value in siblings:
            probed.add(first.value)
    return probed


@pytest.mark.timeout(15)
def test_no_module_outside_the_owner_probes_a_sibling_itself() -> None:
    """The provisioning fact has one spelling: ``probe_provisioning``."""
    violators: list[str] = []
    for path in _maestro_sources():
        if path.name == _OWNER:
            continue
        probed = _probes_a_sibling(path.read_text(encoding="utf-8"))
        if probed:
            violators.append(f"{path.name}:{sorted(probed)}")

    assert not violators, (
        "a first-party sibling is probed with find_spec outside "
        f"maestro/{_OWNER} (proposal 133):\n  " + "\n  ".join(violators)
    )


def test_sibling_probe_guard_flags_a_synthetic_rival_check() -> None:
    """A hand-rolled rival probe is detected, in either call spelling (control)."""
    bare = 'spec = find_spec("maestro_sdk.mcp_config")\n'
    attribute = 'spec = importlib.util.find_spec("maestro_review")\n'

    assert _probes_a_sibling(bare) == {"maestro_sdk.mcp_config"}
    assert _probes_a_sibling(attribute) == {"maestro_review"}
    assert _probes_a_sibling('spec = find_spec("json")\n') == set()
    assert _probes_a_sibling('spec = find_spec(name)\n') == set()
    # A callable with no statically-known name is not the rival spelling.
    assert _probes_a_sibling('spec = finders[0]("maestro_sdk")\n') == set()


def test_the_sibling_name_set_is_non_empty() -> None:
    """The guard cannot pass vacuously: the registry still declares siblings.

    Every name the detector matches on comes from ``_SIBLINGS``, so an emptied
    registry would silently make the walk above unable to find any violator.
    """
    assert _sibling_names() >= {"maestro_sdk", "maestro_sdk.mcp_config", "maestro_review"}


def test_kind_set_guard_flags_a_synthetic_restatement() -> None:
    """A restated vocabulary is detected, a partial mention is not (control)."""
    seeded = 'Kind = Literal["SOUND", "UNPROVISIONED", "UNCOLLECTABLE", "UNFIT"]\n'

    assert _restates_the_kind_set(seeded)
    assert not _restates_the_kind_set('kind = "SOUND"\n')


# The retired red-root carve-out's spellings of "maestro_sdk is not installed":
# its two mypy output anchors and its blocker token. Provisioning is the owner's
# fact, measured by ``find_spec``; a module parsing gate text for it is a rival.
_PROVISIONING_MARKERS: tuple[str, ...] = (
    'module named "maestro_sdk',
    'Module "maestro_sdk" has no attribute',
    "PROVISIONING-GAP",
)


def _spells_a_provisioning_marker(source: str) -> bool:
    """Whether any string constant in *source* carries a provisioning marker."""
    return any(
        marker in constant
        for constant in _string_constants(source)
        for marker in _PROVISIONING_MARKERS
    )


@pytest.mark.timeout(15)
def test_no_module_outside_the_owner_spells_a_provisioning_marker() -> None:
    """Only the owner may decide a red is a missing ``maestro_sdk`` install."""
    violators = [
        path.name
        for path in _maestro_sources()
        if path.name != _OWNER
        and _spells_a_provisioning_marker(path.read_text(encoding="utf-8"))
    ]

    assert not violators, (
        "a maestro_sdk provisioning marker is spelled outside "
        f"maestro/{_OWNER} (proposal 133): {', '.join(violators)}"
    )


def test_provisioning_marker_guard_flags_a_seeded_rival() -> None:
    """A restated marker is flagged; a bare name or ``find_spec`` probe is not."""
    seeded = "_MARKERS = ('module named \"maestro_sdk',)\n"

    assert _spells_a_provisioning_marker(seeded)
    assert not _spells_a_provisioning_marker('name = "maestro_sdk"\n')
    assert not _spells_a_provisioning_marker(
        'find_spec("maestro_sdk.mcp_config")\n',
    )


def _first_party_roots() -> frozenset[str]:
    """The repo-root directory names that are real sibling distributions.

    A directory under :data:`REPO_ROOT` other than ``maestro``/``tests`` that
    carries a ``pyproject.toml`` — the same predicate
    ``tests/test_packaging_boundary.py``'s
    ``test_pinned_siblings_are_real_sibling_projects`` applies by hand, but
    discovering rather than asserting, so ``scripts/``, ``docs/``, ``deploy/``,
    ``data/`` and ``nginx/`` fall out with no exclusion list to maintain.
    """
    return frozenset(
        entry.name
        for entry in REPO_ROOT.iterdir()
        if entry.is_dir() and entry.name not in _NOT_SIBLINGS and (entry / "pyproject.toml").is_file()
    )


def _imported_roots(tree: ast.AST) -> set[str]:
    """Top-level module names imported by *tree* (absolute imports only)."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        # Relative imports (``level > 0``) have no top-level module name and
        # stay within the package, so they cannot reach a sibling distribution.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _unregistered_first_party(
    sources: dict[str, str],
    first_party: frozenset[str],
    registered: frozenset[str],
) -> dict[str, set[str]]:
    """Each source path mapped to the first-party roots the registry omits.

    Pure over a ``{relative path: source text}`` map — no filesystem access — so
    the seeded control can drive every branch in memory.
    """
    offenders: dict[str, set[str]] = {}
    for rel, source in sources.items():
        missing = _imported_roots(ast.parse(source, filename=rel)) & first_party
        missing -= registered
        if missing:
            offenders[rel] = missing
    return offenders


def _registered_siblings() -> frozenset[str]:
    """The packages the oracle's registry covers."""
    return frozenset(package for package, _ in ge._SIBLINGS)


def _submodule_candidates(package: str, submodule: str) -> list[Path]:
    """The on-disk modules a registry pair's *submodule* could resolve to."""
    # The dotted name already starts with the package, so it is relative to the
    # sibling's *import* root: the repo-root directory for a flat layout
    # (``maestro_review/review.py``), or its ``src/`` for a src layout
    # (``maestro_sdk/src/maestro_sdk/mcp_config.py``).
    relative = Path(*submodule.split("."))
    roots = (REPO_ROOT / relative, REPO_ROOT / package / "src" / relative)
    return [path for root in roots for path in (root.with_suffix(".py"), root / "__init__.py")]


def _unresolved_submodules(
    pairs: tuple[tuple[str, str], ...],
    exists: Callable[[Path], bool],
) -> dict[str, list[Path]]:
    """Each unresolvable registry pair mapped to the paths that were searched.

    Pure over an injected *exists* predicate — no filesystem access of its own —
    so the seeded control can drive both branches in memory.
    """
    unresolved: dict[str, list[Path]] = {}
    for package, submodule in pairs:
        candidates = _submodule_candidates(package, submodule)
        if not any(exists(candidate) for candidate in candidates):
            unresolved[f"{package}:{submodule}"] = candidates
    return unresolved


@pytest.mark.timeout(15)
def test_every_first_party_test_import_is_registered() -> None:
    """A sibling the first-party trees import but the oracle never checks is a gap."""
    sources = {
        str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8")
        for root in _WALKED_ROOTS
        for path in sorted(root.rglob("*.py"))
    }
    assert sources, "no first-party sources were collected"
    # Non-vacuity: a discriminator that stopped matching would let an empty
    # first-party set pass the assertion below silently.
    first_party = _first_party_roots()
    assert first_party == {"maestro_sdk", "maestro_review"}, (
        "the set of sibling distributions at the repo root moved; a new one must "
        "be added to both this pin and maestro/"
        f"{_OWNER}'s _SIBLINGS, or probe_provisioning will not check it: "
        f"{sorted(first_party)}"
    )

    offenders = _unregistered_first_party(sources, first_party, _registered_siblings())

    assert not offenders, (
        "a first-party tree imports a sibling distribution that "
        f"maestro/{_OWNER}'s _SIBLINGS does not cover, so probe_provisioning "
        "reports SOUND while collection aborts on it (proposal 133). Add a "
        '("<package>", "<package>.<submodule>") pair to _SIBLINGS for: '
        + "; ".join(f"{rel} -> {sorted(missing)}" for rel, missing in sorted(offenders.items()))
    )


def test_registry_guard_flags_a_synthetic_unregistered_import() -> None:
    """A function-scope import of an unregistered sibling is detected (control).

    The offending import is built by string concatenation, as
    ``tests/test_gate_lease_owner.py``'s text control does, because this guard
    walks ``tests/`` and therefore parses its own source — a literal
    unregistered import written plainly here would make the guard file its own
    offender.
    """
    seeded_import = "from maestro" + "_extra import x"
    seeded = "def test_thing() -> None:\n    " + seeded_import + "\n    import pytest\n"
    first_party = frozenset({"maestro_sdk", "maestro_review", "maestro" + "_extra"})
    # Seeded registry too: a hermetic control must not depend on live state.
    registered = frozenset({"maestro_sdk", "maestro_review"})

    offenders = _unregistered_first_party({"test_seeded.py": seeded}, first_party, registered)

    assert offenders == {"test_seeded.py": {"maestro" + "_extra"}}
    # Negative half: third-party and registered imports are outside the domain.
    assert (
        _unregistered_first_party(
            {"test_clean.py": "import pytest\nimport maestro_sdk\n"},
            first_party,
            registered,
        )
        == {}
    )


@pytest.mark.timeout(15)
def test_every_registered_submodule_resolves_on_disk() -> None:
    """The symmetric failure: a renamed submodule would pin UNPROVISIONED forever.

    ``probe_provisioning`` reports ``UNPROVISIONED`` when a registry pair's
    declared submodule does not resolve, so a submodule renamed away would make
    a perfectly sound environment read as broken.
    """
    unresolved = _unresolved_submodules(ge._SIBLINGS, Path.is_file)

    assert not unresolved, (
        f"maestro/{_OWNER}'s _SIBLINGS declares a submodule with no module on "
        "disk, which would pin probe_provisioning to UNPROVISIONED: "
        + "; ".join(f"{pair} (searched {[str(path) for path in paths]})" for pair, paths in sorted(unresolved.items()))
    )


def test_submodule_guard_flags_a_synthetic_missing_submodule() -> None:
    """A registry pair naming a module that is not on disk is reported (control)."""
    bogus = (("maestro" + "_bogus", "maestro" + "_bogus.nope"),)

    unresolved = _unresolved_submodules(bogus, Path.is_file)

    assert list(unresolved) == ["maestro_bogus:maestro_bogus.nope"]
    assert unresolved["maestro_bogus:maestro_bogus.nope"]
    # Negative half: a predicate that finds everything reports nothing.
    assert _unresolved_submodules(bogus, lambda _path: True) == {}


@pytest.mark.timeout(15)
def test_the_seam_consults_the_oracle_and_never_probes_it() -> None:
    """The lease seam reads the cached verdict; a refresh there would recurse."""
    seam = (MAESTRO_ROOT / _SEAM).read_text(encoding="utf-8")
    called = _called_names(seam)

    assert _ALLOWED_CONSULT in called, (
        f"maestro/{_SEAM} no longer consults the runnability oracle "
        f"({_ALLOWED_CONSULT}) — the binding has gone dead (proposal 133)"
    )
    assert _FORBIDDEN_PROBE not in called, (
        f"maestro/{_SEAM} calls {_FORBIDDEN_PROBE}: probing from inside the "
        "admission seam runs the collector, which runs through the seam "
        "(proposal 133)"
    )


def test_consult_guard_flags_a_synthetic_probe() -> None:
    """A seeded refresh call is detected in both spellings (seeded-fault control)."""
    assert _FORBIDDEN_PROBE in _called_names("x = refresh_verdict(path)\n")
    assert _FORBIDDEN_PROBE in _called_names(
        "x = gate_environment.refresh_verdict(path)\n",
    )
    assert _ALLOWED_CONSULT not in _called_names("x = refresh_verdict(path)\n")
    assert _ALLOWED_CONSULT in _called_names("x = ge.read_verdict(path)\n")


# The approved-plan selector, ``maestro``-relative, and the function inside it
# that must read the oracle before an implementation attempt is spent.
_SELECTOR_MODULE = "loop/implementation.py"
_SELECTOR = "_implement_approved_plans"

# The consult the selector makes, and the spend it guards. Both are matched on
# the trailing name, so ``plan_holds.gate_environment_verdict(...)`` and
# ``self.implement_plan(...)`` reduce to the same rule as the bare spellings.
_PRE_SPEND_CONSULT = "gate_environment_verdict"
_SPEND_CALL = "implement_plan"


def _find_function(
    tree: ast.Module, name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the function named *name* anywhere in *tree*, or raise.

    Both flavours are matched: the selector is an ``async def``, and a lookup
    that silently missed one would make the rule below vacuous.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"function not found: {name}")


def _pre_spend_consult_broken(source: str, name: str = _SELECTOR) -> str | None:
    """Why *source*'s selector no longer consults the oracle first, else ``None``.

    The binding is an *ordering*: the first ``gate_environment_verdict`` call
    must sit strictly above every ``implement_plan`` call inside the selector.
    A consult moved below the spend, or deleted outright, reads the same to a
    name-only guard, so the line numbers are compared rather than the presence.
    """
    node = _find_function(ast.parse(source), name)
    consults = [
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and _called_name(call.func) == _PRE_SPEND_CONSULT
    ]
    spends = [
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and _called_name(call.func) == _SPEND_CALL
    ]
    if not spends:
        return f"{name} no longer calls {_SPEND_CALL}"
    if not consults:
        return f"{name} no longer calls {_PRE_SPEND_CONSULT}"
    if min(consults) > min(spends):
        return (
            f"{name} calls {_PRE_SPEND_CONSULT} at line {min(consults)}, "
            f"below {_SPEND_CALL} at line {min(spends)}"
        )
    return None


@pytest.mark.timeout(15)
def test_the_selector_consults_the_oracle_before_it_spends_an_attempt() -> None:
    """The environment hold is pre-spend, or it is not a hold at all."""
    source = (MAESTRO_ROOT / _SELECTOR_MODULE).read_text(encoding="utf-8")
    reason = _pre_spend_consult_broken(source)

    assert reason is None, (
        f"maestro/{_SELECTOR_MODULE}::{_SELECTOR} no longer consults the "
        f"runnability oracle ({_PRE_SPEND_CONSULT}) before it calls "
        f"{_SPEND_CALL} — the binding has gone dead (proposal 133): an approved "
        "plan is then spent, demoted and filed as an issue against a red no "
        f"diff can clear. Reason: {reason}"
    )


def test_pre_spend_guard_flags_a_deleted_or_reordered_consult() -> None:
    """Both ways the ordering rots are detected (seeded-fault control).

    The unmutated tree is asserted clean first, so a detector that had gone
    blind could not make the seeded halves pass vacuously.
    """
    real = (MAESTRO_ROOT / _SELECTOR_MODULE).read_text(encoding="utf-8")
    assert _pre_spend_consult_broken(real) is None

    deleted = (
        "async def _seeded(self) -> None:\n"
        "    for plan in plans:\n"
        "        await self.implement_plan(plan.plan_id)\n"
    )
    reordered = (
        "async def _seeded(self) -> None:\n"
        "    for plan in plans:\n"
        "        await self.implement_plan(plan.plan_id)\n"
        "    environment = plan_holds.gate_environment_verdict(self, iteration)\n"
    )
    ordered = (
        "async def _seeded(self) -> None:\n"
        "    environment = plan_holds.gate_environment_verdict(self, iteration)\n"
        "    for plan in plans:\n"
        "        await self.implement_plan(plan.plan_id)\n"
    )

    assert _pre_spend_consult_broken(deleted, "_seeded") is not None
    assert _PRE_SPEND_CONSULT in str(_pre_spend_consult_broken(deleted, "_seeded"))
    assert _pre_spend_consult_broken(reordered, "_seeded") is not None
    assert "below" in str(_pre_spend_consult_broken(reordered, "_seeded"))
    assert _pre_spend_consult_broken(ordered, "_seeded") is None
    # A selector that no longer spends at all is its own breakage, not a pass.
    assert _pre_spend_consult_broken(
        "async def _seeded() -> None:\n    pass\n", "_seeded",
    ) is not None


# The attribution the convergence terminal may hand to an environment-owned red,
# and the module holding the one function allowed to decide it.
_ENVIRONMENT_ATTRIBUTION = "ENVIRONMENT"
_ATTRIBUTION_OWNER = "gate_observation.py"


def _environment_producer_lines(source: str) -> list[int]:
    """The lines of *source* that ``return`` the environment attribution.

    *Producing* the verdict is a ``return`` of the bare literal. A comparison
    (``attribution == "ENVIRONMENT"``, as ``maestro/evolution.py`` and
    ``maestro/attempt_disposition.py`` spell it) parses as :class:`ast.Compare`
    and a ``Literal[...]`` alias member as :class:`ast.Subscript`, so both stay
    legal — only a second site *deciding* a red is the environment's is caught.
    """
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value == _ENVIRONMENT_ATTRIBUTION
    ]


@pytest.mark.timeout(15)
def test_exactly_one_site_produces_the_environment_attribution() -> None:
    """``attribute`` alone may excuse a red as the environment's."""
    sites: list[str] = []
    for path in _maestro_sources():
        rel = path.relative_to(MAESTRO_ROOT).as_posix()
        sites += [
            f"{rel}:{lineno}"
            for lineno in _environment_producer_lines(path.read_text(encoding="utf-8"))
        ]

    assert len(sites) == 1, (
        f"the {_ENVIRONMENT_ATTRIBUTION} attribution must be produced exactly "
        f"once, by gate_observation.attribute (proposal 133) — a second "
        "producer would let a genuine regression be excused as a broken "
        f"environment. Found: {sorted(sites)}"
    )
    assert sites[0].startswith(f"{_ATTRIBUTION_OWNER}:"), (
        f"the sole {_ENVIRONMENT_ATTRIBUTION} producer moved out of "
        f"maestro/{_ATTRIBUTION_OWNER}: {sites[0]} (proposal 133)"
    )


def test_environment_producer_guard_flags_a_synthetic_second_producer() -> None:
    """A seeded return is caught; a seeded comparison is not (control).

    Every seeded literal is built by concatenation so this file — which the
    sibling-import guard above already parses — never becomes an offender of a
    future walk over ``tests/``, the rule
    ``test_registry_guard_flags_a_synthetic_unregistered_import`` documents.
    """
    literal = '"ENVIRON" "MENT"'
    seeded = "def _classify(x):\n    return " + literal + "\n"
    comparison = "def _classify(x):\n    if x == " + literal + ":\n        return None\n"
    alias = "Attribution = Literal[" + literal + ", " + '"ATTRIBUTED"' + "]\n"

    assert _environment_producer_lines(seeded) == [2]
    assert _environment_producer_lines(comparison) == []
    assert _environment_producer_lines(alias) == []
    assert _environment_producer_lines("def f():\n    return None\n") == []
