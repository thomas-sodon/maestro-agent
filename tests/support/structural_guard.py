"""Reusable AST structural-guard harness (proposal 109).

Maestro's test suite defends its load-bearing invariants with a family of AST
*structural guards* — tests that parse ``maestro/`` source and fail the build
when a merge decision, a CI classification, a salvage verdict, a WIP directive,
or a session-attachment rule is made outside its single sanctioned owner. Those
guards were hand-authored by copy-pasting the previous one: the
``_line_to_function`` line->function mapper is duplicated verbatim across the
chokepoint files, and every guard re-implements the same skeleton — a whole-AST
scan, a ``(module, function)`` allowlist, a synthetic-bypass negative control, a
synthetic-OK positive control, and an allowlist-staleness check.

This module captures that shared skeleton behind one parameterized spec,
:class:`ChokepointGuard`, plus the pure/IO-split operations that drive it. It
touches no existing guard file and adds no registry — the bespoke guards keep
protecting their invariants exactly as before. A later increment seeds a
declarative registry against this harness and retires the copy-pasted files one
at a time, each proven at parity first.

The pure operations (:func:`scan_source`, :func:`assert_flags_synthetic`,
:func:`assert_synthetic_ok`) never touch disk; the file-reading wrappers
(:func:`scan_for_violations`, :func:`symbols_defined_in`,
:func:`functions_by_module`) read the code under guard, which — like
``tests/test_fault_seeding.py`` reading its target module — is the input to the
guard, not test-generated I/O. Every real-tree reader has an injectable override
so unit tests can stay in memory.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Repo-relative root of the package under guard. This file lives at
# ``tests/support/structural_guard.py``, so three ``parent`` hops reach the repo
# root; the bespoke guards at ``tests/test_*.py`` need only two.
_MAESTRO_ROOT = Path(__file__).resolve().parent.parent.parent / "maestro"

# Sentinel enclosing-"function" name for a violation at module scope (no
# enclosing ``def``). ``scan_source`` maps such nodes to this, and a guard that
# needs to allowlist a module-scope occurrence uses it as the function key;
# ``assert_allowlist_entries_real`` treats it as always-valid because it names
# no ``def`` and so never appears in ``functions_by_module``. Both paths share
# this one constant so they cannot disagree on the module-scope key.
_MODULE_SCOPE = "<module>"

# A detector classifies a single AST node: it returns a short primitive
# description for a *violating* node (used verbatim in the violation string),
# else ``None``. Each guard supplies its own classifier while the scan machinery
# below stays shared.
Detector = Callable[[ast.AST], str | None]


def line_to_function(tree: ast.AST) -> dict[int, str]:
    """Map ``lineno`` -> enclosing function name within *tree* (methods included).

    Inner closures resolve to their outer named function (``ast.walk`` is
    breadth-first, so the outer ``FunctionDef`` is seen first and ``setdefault``
    keeps it) — the verbatim shape the bespoke chokepoint guards duplicate.
    """
    line_to_func: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            line = getattr(child, "lineno", None)
            if line is not None:
                line_to_func.setdefault(line, node.name)
    return line_to_func


def line_to_innermost_function(tree: ast.AST) -> dict[int, str]:
    """Map ``lineno`` -> *innermost* enclosing function name within *tree*.

    The counterpart to :func:`line_to_function`, which resolves to the *outer*
    function. Here the deepest enclosing ``FunctionDef``/``AsyncFunctionDef``
    wins: functions are filled widest-span first, so an inner function (whose
    span is strictly contained, hence smaller) is filled last and overwrites its
    encloser's lines. Load-bearing for the live-work guard — a nested closure
    (e.g. ``on_approve``/``on_reject`` in ``plan_decision.telegram_plan_handlers``)
    must resolve to itself, not force a whole-encloser exemption. The live-work registry
    entry (``_LIVE_WORK_GUARD`` in ``tests/structural_guards.py``) is its sole
    consumer, via ``line_resolver``.
    """
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    functions.sort(
        key=lambda fn: (fn.end_lineno or fn.lineno) - fn.lineno,
        reverse=True,
    )
    line_to_func: dict[int, str] = {}
    for fn in functions:
        for line in range(fn.lineno, (fn.end_lineno or fn.lineno) + 1):
            line_to_func[line] = fn.name
    return line_to_func


def scan_paths(*rels: str) -> Callable[[Path], list[Path]]:
    """Resolver over the explicit relative paths under the guarded root.

    The merge-truth chokepoint shape — a fixed tuple of modules where control
    flow is decided (e.g. ``agent.py``, ``loop/pr_remediation.py``).
    """

    def resolve(root: Path) -> list[Path]:
        return [root / rel for rel in rels]

    return resolve


def scan_all_except(*owners: str) -> Callable[[Path], list[Path]]:
    """Resolver over every ``*.py`` under the guarded root minus owner basenames.

    The CI-truth chokepoint shape — the whole package is scanned except the sole
    owner module(s) whose own definitions of the guarded primitive are the point.
    Deterministically ordered.
    """
    owner_set = frozenset(owners)

    def resolve(root: Path) -> list[Path]:
        return sorted(p for p in root.rglob("*.py") if p.name not in owner_set)

    return resolve


def scan_all_except_paths(*rels: str) -> Callable[[Path], list[Path]]:
    """Resolver over every ``*.py`` under the guarded root minus repo-relative paths.

    The path-keyed counterpart to :func:`scan_all_except`. Where that excludes by
    *basename*, this keys on each file's posix path *relative to the guarded root*,
    so it can exempt a single file (``"sdk.py"`` -> only ``maestro/sdk.py``) or a
    whole subtree (a ``rel`` ending in ``/``, ``"providers/"`` -> every file under
    ``maestro/providers/``) without over-excluding a basename collision elsewhere in
    the tree (``notifications/base.py`` survives a ``"providers/"`` exclusion because
    it is keyed by path, not name). The vendor-SDK-import guard needs this: its owner
    surface is the ``providers/`` subtree, which a basename exclusion cannot express
    (``base.py`` also lives under ``notifications/``). Deterministically ordered,
    matching :func:`scan_all_except`.
    """
    file_owners = frozenset(rel for rel in rels if not rel.endswith("/"))
    subtree_owners = tuple(rel for rel in rels if rel.endswith("/"))

    def resolve(root: Path) -> list[Path]:
        kept: list[Path] = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel in file_owners:
                continue
            if any(rel.startswith(prefix) for prefix in subtree_owners):
                continue
            kept.append(path)
        return sorted(kept)

    return resolve


@dataclass(frozen=True)
class PresenceRequirement:
    """Declarative spec for a required-import *presence* assertion.

    Where a :class:`ChokepointGuard`'s detector flags a *violating* node, a
    presence requirement asserts the inverse: each module in ``targets`` (paths
    relative to the guarded root, like :func:`scan_paths`' arguments) **must**
    contain ``from <import_module> import <import_name>``. ``name`` is a
    human-readable label and ``message`` the trailing explanation appended when a
    module is missing the import. The vendor-session invariant uses it to require
    every loop consumer to hold the opaque ``maestro.providers.base.Session``
    handle — the presence counterpart the single-node detector cannot express.
    """

    name: str
    targets: tuple[str, ...]
    import_module: str
    import_name: str
    message: str


@dataclass(frozen=True)
class ScopedReference:
    """Declarative spec for a *function-scoped* reference requirement.

    Where a :class:`PresenceRequirement` asserts a required import at module
    scope, a scoped reference asserts that a :data:`Detector` matches (or does not
    match) *inside one named function of one module*. ``module`` is a path relative
    to the guarded root (like :func:`scan_paths`' arguments) and ``function`` the
    ``def`` / ``async def`` name to scope to; ``detector`` classifies each node in
    that function's subtree, ``must_be_present`` chooses the polarity (a hit is
    required when ``True``, forbidden when ``False``), ``name`` is a human-readable
    label and ``message`` the trailing explanation appended when the requirement is
    unmet. The origin-grounded tend entry uses three: the grounded observer is
    *present* inside ``tend_evolution_prs``, the host-proxy conflict helper is
    *absent* from it, and the ``reset_hard("origin/main")`` heal call is *present* —
    the three in-function checks the single-node whole-tree scan cannot express.
    """

    name: str
    module: str
    function: str
    detector: Detector
    must_be_present: bool
    message: str


@dataclass(frozen=True)
class CoOccurrenceRequirement:
    """Declarative spec for a *scope-level co-occurrence* requirement.

    Where a :class:`ScopedReference` asserts a detector holds inside *one named*
    function, a co-occurrence requirement asserts a relationship across *every*
    function of a module: in each scope where ``trigger`` fires, ``companion`` must
    fire too. ``module`` is a path relative to the guarded root (like
    :func:`scan_paths`' arguments); ``trigger`` and ``companion`` are
    :data:`Detector`\\ s classifying each node; ``expected_scopes``, when not
    ``None``, locks the exact set of function names where ``trigger`` fires (a new or
    vanished trigger scope then fails loud, forcing a reviewed update); ``allowlist``
    names functions exempt from the companion requirement (a trigger scope that
    legitimately never consults the companion); ``name`` is a human-readable label and
    ``message`` the trailing explanation appended when the requirement is unmet. The
    salvage entry uses two: every ``agent.py`` disengage site that references a
    ``Salvage`` verdict must also call the oracle gatherer (``expected_scopes`` locked
    to the four curated sites), and every give-up author must consult the oracle before
    persisting a stand-down (``_mechanical_standdown_verdict`` allowlisted) — the two
    scope-level co-occurrence rules the single-node detector model cannot express.
    """

    name: str
    module: str
    trigger: Detector
    companion: Detector
    expected_scopes: frozenset[str] | None
    allowlist: frozenset[str]
    message: str


@dataclass(frozen=True)
class PartitionCoOccurrence:
    """Declarative spec for a *scope-aggregate* partition co-occurrence requirement.

    Where a :class:`CoOccurrenceRequirement` asserts a relationship over the functions
    of *one* module, a partition requirement is a *tree-wide* rule with a scope-aggregate
    trigger the single-node detectors cannot express: across every scanned module, any
    scope that names ``pivot`` **plus at least** ``min_distinct`` distinct reason codes
    (so it *decides* the owned/external partition, not merely mentions one code) must
    consult the ``companion`` in that same scope. ``code_detector`` is a :data:`Detector`
    returning a canonical reason-code string per node (else ``None``); ``pivot`` is the
    one code whose ownership the rollout flag moves; ``min_distinct`` (default 2) is the
    distinct-code floor a scope must reach to count as a partition; ``companion`` is the
    source-consultation detector (a hit anywhere in the scope licenses the grouping);
    ``allowlist`` names ``(module_basename, function)`` scopes exempt from the companion
    requirement (a producer of codes rather than a consumer of ownership);
    ``exclude_modules`` is the set of basenames never scanned (the taxonomy's own source
    module, entitled to enumerate the codes because it *defines* them); ``name`` is a
    human-readable label and ``message`` the trailing explanation appended when the
    requirement is unmet. The blocker-ownership entry uses one: no consumer outside
    ``remediation_tracker.py`` may re-derive the owned/external split from a private
    reason-code list — the scope-aggregate ("names the pivot plus another code without
    consulting the classifier") rule neither the whole-tree single-node scan nor the
    single-module :class:`CoOccurrenceRequirement` can express.
    """

    name: str
    code_detector: Detector
    pivot: str
    companion: Detector
    allowlist: frozenset[tuple[str, str]]
    exclude_modules: frozenset[str]
    message: str
    min_distinct: int = 2


@dataclass(frozen=True)
class ModulePurity:
    """Declarative spec for a *module-scoped forbidden* import-plus-reference rule.

    Where a :class:`PresenceRequirement` asserts a required import is *present* at
    module scope, a module-purity spec asserts the inverse *absence* across a whole
    module: ``module`` (a path relative to the guarded root, like :func:`scan_paths`'
    arguments) must neither import the ``forbidden_import_prefix`` dotted package
    (itself or any submodule, in any spelling — ``import``, ``from pkg import mod`` or
    relative — resolved via :func:`imported_modules`) nor reference any name in
    ``forbidden_names`` (a bare :class:`ast.Name` id or an :class:`ast.Attribute`
    attr, anywhere in the module).
    ``name`` is a human-readable label and ``message`` the trailing explanation
    appended when the module is impure. The push-permission entry uses one: the
    enforcement gate ``tool_gating.py`` consumes the injected ``allowed_branches`` set
    and must never import from ``maestro.pr_facts`` nor name an ownership primitive to
    re-derive it — the module-scoped forbidden rule neither the whole-tree call scan
    nor the function-scoped :class:`ScopedReference` can express.
    """

    name: str
    module: str
    forbidden_import_prefix: str
    forbidden_names: frozenset[str]
    message: str


@dataclass(frozen=True)
class ChokepointGuard:
    """Declarative spec for one AST structural guard.

    ``subjects`` names the symbol(s) the guard protects (so a deleted subject can
    turn the guard red instead of leaving it silently guarding nothing);
    ``scanned`` resolves the guarded root to the files to walk; ``detector``
    classifies violating nodes; ``allowlist`` exempts ``(module_basename,
    function)`` pairs; ``synthetic_bypass`` / ``synthetic_ok`` are the negative
    and positive controls that keep the guard's own machinery honest;
    ``line_resolver`` chooses how a violating node's line resolves to an enclosing
    function — the default :func:`line_to_function` (outermost) matches the four
    original entries, while :func:`line_to_innermost_function` lets an entry
    attribute a nested closure to itself rather than its encloser.
    ``exempt_lines`` is an optional per-tree hook: when supplied it is called once
    per parsed tree and returns the line numbers whose otherwise-violating node is
    suppressed — the session-attachment guard uses it to bless a fallback-branch
    ``await query(...)`` without exempting the whole enclosing phase function.
    Default ``None`` yields an empty exempt set, so every entry that omits it is
    unaffected. ``required_presence`` carries optional
    :class:`PresenceRequirement` specs — the *inverse* of the violation scan,
    asserting a required import is present in each named module (the
    vendor-session guard requires every loop consumer to hold the opaque
    ``Session`` handle). Default ``()`` leaves every entry that omits it unchecked
    by the presence runner. ``scoped_references`` carries optional
    :class:`ScopedReference` specs — a detector required (or forbidden) *inside one
    named function* of one module (the origin-grounded entry requires the grounded
    observer inside ``tend_evolution_prs`` and forbids the host-proxy helper there).
    Default ``()`` leaves every entry that omits it unchecked by the scoped runner.
    ``co_occurrences`` carries optional :class:`CoOccurrenceRequirement` specs — a
    scope-level rule that every function where a *trigger* detector fires also matches
    a *companion* detector (the salvage entry requires every disengage site that
    references a ``Salvage`` verdict, and every give-up author, to consult the oracle).
    Default ``()`` leaves every entry that omits it unchecked by the co-occurrence runner.
    ``module_purity`` carries optional :class:`ModulePurity` specs — a module-scoped
    *forbidden* rule that a named module neither imports a dotted prefix nor references
    any forbidden name (the push-permission entry pins the enforcement gate's purity
    over its injected ``allowed_branches`` set). Default ``()`` leaves every entry that
    omits it unchecked by the module-purity runner. ``partition_co_occurrences`` carries
    optional :class:`PartitionCoOccurrence` specs — a tree-wide scope-aggregate rule that
    no scope naming a pivot code plus another distinct code decides the owned/external
    partition without consulting the classifier (the blocker-ownership entry pins its
    one-source partition scan). Default ``()`` leaves every entry that omits it unchecked
    by the partition-co-occurrence runner.
    """

    name: str
    provenance: str
    subjects: tuple[str, ...]
    scanned: Callable[[Path], list[Path]]
    detector: Detector
    allowlist: frozenset[tuple[str, str]]
    synthetic_bypass: str
    synthetic_ok: str
    line_resolver: Callable[[ast.AST], dict[int, str]] = line_to_function
    exempt_lines: Callable[[ast.AST], set[int]] | None = None
    required_presence: tuple[PresenceRequirement, ...] = ()
    scoped_references: tuple[ScopedReference, ...] = ()
    co_occurrences: tuple[CoOccurrenceRequirement, ...] = ()
    module_purity: tuple[ModulePurity, ...] = ()
    partition_co_occurrences: tuple[PartitionCoOccurrence, ...] = ()


def scan_source(
    source: str,
    detector: Detector,
    allowlist: frozenset[tuple[str, str]],
    module_base: str,
    *,
    line_resolver: Callable[[ast.AST], dict[int, str]] = line_to_function,
    exempt_lines: Callable[[ast.AST], set[int]] | None = None,
) -> list[str]:
    """Walk *source*, apply *detector*, return allowlist-filtered violations.

    Pure — no disk. Each violating node maps to its enclosing function via
    *line_resolver* (:func:`line_to_function` by default, or
    :func:`line_to_innermost_function` for an entry that must attribute nested
    closures to themselves); a hit whose ``(module_base, function)`` pair is in
    *allowlist* is suppressed. When *exempt_lines* is supplied it is called once
    on the parsed tree, and any otherwise-violating node whose ``lineno`` is in the
    returned set is suppressed too (the per-line exemption the session-attachment
    guard needs for its blessed fallback branch); ``None`` means no exemption.
    Violations read ``"<module_base>:<line> — <primitive> in <function>"``.
    """
    return [
        f"{module_base}:{line} — {primitive} in {func_name}"
        for line, func_name, primitive in _iter_hits(
            source, detector, line_resolver=line_resolver, exempt_lines=exempt_lines
        )
        if (module_base, func_name) not in allowlist
    ]


def _iter_hits(
    source: str,
    detector: Detector,
    *,
    line_resolver: Callable[[ast.AST], dict[int, str]],
    exempt_lines: Callable[[ast.AST], set[int]] | None,
) -> Iterator[tuple[int, str, str]]:
    """Yield ``(line, function, primitive)`` for every non-exempt detector hit.

    The allowlist-free walk :func:`scan_source` filters and
    :func:`allowlisted_hits` aggregates, so both share one hit definition.
    """
    tree = ast.parse(source)
    mapping = line_resolver(tree)
    exempt = exempt_lines(tree) if exempt_lines is not None else set()
    for node in ast.walk(tree):
        primitive = detector(node)
        if primitive is None:
            continue
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        if line in exempt:
            continue
        yield line, mapping.get(line, _MODULE_SCOPE), primitive


def scan_for_violations(spec: ChokepointGuard, root: Path = _MAESTRO_ROOT) -> list[str]:
    """Resolve *spec*'s scanned files under *root*, read each, and scan it.

    The file-reading wrapper around :func:`scan_source`; the allowlist and the
    violation-string prefix both key on each file's basename.
    """
    violators: list[str] = []
    for path in spec.scanned(root):
        source = path.read_text(encoding="utf-8")
        violators.extend(
            scan_source(
                source,
                spec.detector,
                spec.allowlist,
                path.name,
                line_resolver=spec.line_resolver,
                exempt_lines=spec.exempt_lines,
            )
        )
    return violators


def assert_flags_synthetic(spec: ChokepointGuard) -> None:
    """Negative control: *spec*'s ``synthetic_bypass`` must flag at least one node.

    A bypass that flags nothing means the detector no longer catches its own
    known violation — the guard has gone silently dead. The ``(module, function)``
    allowlist is *not* applied, so the control tests the detector in isolation; the
    per-line ``exempt_lines`` hook *is* applied (a bypass is an unconditional
    violation, never in a blessed fallback branch, so it still flags).
    """
    violators = scan_source(
        spec.synthetic_bypass,
        spec.detector,
        frozenset(),
        "<synthetic>",
        line_resolver=spec.line_resolver,
        exempt_lines=spec.exempt_lines,
    )
    assert violators, (
        f"guard {spec.name}: synthetic_bypass flagged nothing — the detector no "
        "longer catches its own known bypass, so the guard is silently dead"
    )


def assert_synthetic_ok(spec: ChokepointGuard) -> None:
    """Positive control: *spec*'s ``synthetic_ok`` must flag nothing.

    An OK snippet that flags means the detector over-fires on a legitimate
    pattern. The ``(module, function)`` allowlist is *not* applied, but the
    per-line ``exempt_lines`` hook *is* — so a detector-positive node is tolerated
    only when it sits on an exempt line (e.g. the session guard's blessed
    fallback-branch ``await query(...)``); any non-exempt positive node flags.
    """
    violators = scan_source(
        spec.synthetic_ok,
        spec.detector,
        frozenset(),
        "<synthetic>",
        line_resolver=spec.line_resolver,
        exempt_lines=spec.exempt_lines,
    )
    assert not violators, (
        f"guard {spec.name}: synthetic_ok flagged {violators} — the detector "
        "over-fires on a legitimate pattern"
    )


def _target_names(target: ast.expr) -> set[str]:
    """Names bound by an assignment *target* (simple, tuple, or list unpack)."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for elt in target.elts for name in _target_names(elt)}
    return set()


def _collect_symbols(node: ast.AST, names: set[str], *, in_function: bool) -> None:
    """Recurse *node*, adding def/class names (any nesting) and assignment
    targets bound outside function scope (module- and class-level constants)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(child.name)
            _collect_symbols(child, names, in_function=True)
        elif isinstance(child, ast.ClassDef):
            # A class body is not a function scope: its attributes stay eligible
            # unless the class itself is nested inside a function.
            names.add(child.name)
            _collect_symbols(child, names, in_function=in_function)
        elif not in_function and isinstance(child, ast.Assign):
            for tgt in child.targets:
                names |= _target_names(tgt)
            _collect_symbols(child, names, in_function=in_function)
        elif not in_function and isinstance(child, ast.AnnAssign):
            names |= _target_names(child.target)
            _collect_symbols(child, names, in_function=in_function)
        else:
            _collect_symbols(child, names, in_function=in_function)


def bound_symbol_names(tree: ast.AST) -> set[str]:
    """Every name *tree* binds at module or class scope.

    Function/method/class ``def`` names are collected at any nesting (as before);
    ``Assign``/``AnnAssign`` targets are collected only outside function scope, so
    a guard ``subject`` naming a module-level constant — a registry list like
    ``GATE_CHAIN`` or a sentinel — resolves instead of falsely tripping
    :func:`assert_subjects_exist`'s retirement trigger, while a function-local
    name never masks a genuinely-deleted subject.
    """
    names: set[str] = set()
    _collect_symbols(tree, names, in_function=False)
    return names


def symbols_defined_in(root: Path) -> set[str]:
    """Every function, method, class, and module/class-level bound name under *root*.

    Delegates per file to :func:`bound_symbol_names`, so registry lists and
    sentinel constants count as defined symbols alongside ``def``/``class`` names.
    """
    defined: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined |= bound_symbol_names(tree)
    return defined


def functions_by_module(paths: list[Path]) -> dict[str, set[str]]:
    """Map each file's basename -> the set of function/method names it defines."""
    by_module: dict[str, set[str]] = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        by_module.setdefault(path.name, set()).update(names)
    return by_module


def assert_allowlist_entries_real(
    spec: ChokepointGuard,
    by_module: dict[str, set[str]] | None = None,
    root: Path = _MAESTRO_ROOT,
) -> None:
    """Every allowlist ``(module, function)`` must name a live function.

    A stale allowlist entry silently widens the guard's blind spot. *by_module*
    is injectable (defaults to reading ``spec.scanned(root)``) so unit tests can
    stay in memory.
    """
    if by_module is None:
        by_module = functions_by_module(spec.scanned(root))
    stale = [
        f"{module}:{func}"
        for module, func in sorted(spec.allowlist)
        # ``_MODULE_SCOPE`` is the key ``scan_source`` assigns a module-level
        # occurrence; it names no ``def`` so ``functions_by_module`` never lists
        # it. Treat it as always-valid so the two paths agree on what a valid
        # allowlist entry is (a module-scope entry honored by the scanner must
        # not be reported stale here).
        if func != _MODULE_SCOPE and func not in by_module.get(module, set())
    ]
    assert not stale, (
        f"guard {spec.name}: allowlist names functions that no longer exist — "
        f"remove or update them: {', '.join(stale)}"
    )


def allowlisted_hits(
    spec: ChokepointGuard,
    root: Path = _MAESTRO_ROOT,
    sources: dict[str, str] | None = None,
) -> set[tuple[str, str]]:
    """Every ``(module_base, function)`` the scan would flag with no allowlist.

    Reads ``spec.scanned(root)`` — or, when *sources* is supplied, the in-memory map
    keyed by module basename — and keeps ``line_resolver`` / ``exempt_lines``, so the
    result is exactly the set of pairs an allowlist entry can shield.
    """
    pairs: list[tuple[str, str]] = (
        list(sources.items())
        if sources is not None
        # Pairs, not a dict: distinct files may share a basename (``__init__.py``).
        else [(path.name, path.read_text(encoding="utf-8")) for path in spec.scanned(root)]
    )
    return {
        (base, func_name)
        for base, source in pairs
        for _, func_name, _ in _iter_hits(
            source,
            spec.detector,
            line_resolver=spec.line_resolver,
            exempt_lines=spec.exempt_lines,
        )
    }


def assert_allowlist_entries_live(
    spec: ChokepointGuard,
    root: Path = _MAESTRO_ROOT,
    sources: dict[str, str] | None = None,
) -> None:
    """Every allowlist ``(module, function)`` must exempt at least one live hit.

    An entry whose scope no longer contains a detector hit is a dead, function-wide
    blind spot: the forbidden pattern could be reintroduced there silently. The
    liveness counterpart to :func:`assert_allowlist_entries_real` (which only asks
    whether the ``def`` exists). *sources* is injectable, keyed by basename.
    """
    hits = allowlisted_hits(spec, root=root, sources=sources)
    dead = [f"{module}:{func}" for module, func in sorted(spec.allowlist - hits)]
    assert not dead, (
        f"guard {spec.name}: allowlist entry exempts no live hit — delete the entry "
        f"and its justifying comment: {', '.join(dead)}"
    )


def assert_subjects_exist(spec: ChokepointGuard, defined: set[str] | None = None) -> None:
    """Every ``spec.subjects`` entry must still name a symbol in ``maestro/``.

    This is the retirement trigger: a guard whose protected subject was deleted
    must fail the build rather than guard nothing. A subject may be qualified
    (``Class.method``); the final dotted component is matched. *defined* is
    injectable (defaults to :func:`symbols_defined_in` over ``maestro/``).
    """
    if defined is None:
        defined = symbols_defined_in(_MAESTRO_ROOT)
    for subject in spec.subjects:
        name = subject.rsplit(".", 1)[-1]
        assert name in defined, (
            f"guard {spec.name} protects {subject}, which no longer exists — "
            "retire this entry or update its subject"
        )


def _imports_from(tree: ast.AST, module: str, name: str) -> bool:
    """Return whether *tree* contains ``from <module> import <name>``.

    The general "``from <module> import <name>`` is present" predicate, ported
    from the retired ``tests/test_provider_session_handle.py`` — the inverse of a
    violation scan, driving :func:`assert_required_presence`.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == module
            and any(alias.name == name for alias in node.names)
        ):
            return True
    return False


def imported_modules(tree: ast.AST, package: str) -> set[str]:
    """Return every fully-qualified module an import statement in *tree* may bind.

    Expands each spelling to dotted module names so a prefix match cannot be dodged
    by syntax: ``import a.b`` yields ``a.b``; ``from a import b, c`` yields ``a``,
    ``a.b`` and ``a.c`` (an imported name may be a submodule, so it is included
    conservatively; ``*`` adds nothing beyond ``a``); a relative ``from ..x import y``
    resolves its base against *package* — the dotted package the importing module
    lives in — by dropping ``level - 1`` trailing components before the same
    expansion.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                if node.module:
                    parts.append(node.module)
                base = ".".join(parts)
            else:
                base = node.module or ""
            modules.add(base)
            modules.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return modules


def imports_from_prefix(tree: ast.AST, prefix: str, package: str) -> bool:
    """Return whether *tree* imports ``prefix`` or any submodule, in any spelling.

    The prefix generalization of :func:`_imports_from`, matched over the resolved
    set :func:`imported_modules` returns for *package*: a resolved module that
    *equals* ``prefix`` or begins with ``prefix + "."`` counts — so ``import
    maestro.pr_facts``, ``from maestro import pr_facts``, ``from . import pr_facts``
    and a submodule import are all caught, while a same-prefixed sibling
    (``maestro.pr_facts_other``) and the parent package are not. Drives the import
    half of :func:`assert_module_purity`.
    """
    return any(
        module == prefix or module.startswith(prefix + ".")
        for module in imported_modules(tree, package)
    )


def assert_required_presence(
    requirement: PresenceRequirement,
    root: Path = _MAESTRO_ROOT,
    sources: dict[str, str] | None = None,
) -> None:
    """Assert every ``requirement.targets`` module carries the required import.

    Reads each target module under *root* (mirroring :func:`symbols_defined_in`'s
    ``read_text`` reader) — or, when *sources* is supplied, looks the source up in
    that in-memory map keyed by the same relative target path, so unit tests can
    stay off disk. On the disk path a missing target file fails with a curated
    ``target module ... no longer exists`` message — the retirement trigger
    :func:`assert_subjects_exist` gives for a deleted subject — instead of a raw
    ``FileNotFoundError``, so a renamed/deleted consumer points at the entry to
    retire or update. Fails naming the required import, every module still missing
    it, and the requirement's ``message``. The presence counterpart to
    :func:`assert_subjects_exist`.
    """
    missing: list[str] = []
    for target in requirement.targets:
        if sources is not None:
            source = sources[target]
        else:
            path = root / target
            assert path.exists(), (
                f"guard requirement {requirement.name}: target module {target} "
                "no longer exists — retire or update this entry"
            )
            source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=target)
        if not _imports_from(tree, requirement.import_module, requirement.import_name):
            missing.append(target)
    assert not missing, (
        f"guard requirement {requirement.name}: modules missing "
        f"``from {requirement.import_module} import {requirement.import_name}``:\n  "
        + "\n  ".join(missing)
        + f"\n\n{requirement.message}"
    )


def name_reference_detector(symbol: str) -> Detector:
    """A :data:`Detector` that flags any reference to *symbol*.

    Matches a bare :class:`ast.Name` whose ``id`` is *symbol* (``f(...)`` / ``f``)
    or an :class:`ast.Attribute` whose ``attr`` is *symbol* (``self.f(...)`` /
    ``x.f``), so a call, a plain reference, or a passed-as-argument use of the
    symbol is all caught — the two node shapes the retired origin-grounded bespoke
    guard collected in its ``_names_in``. Returns the matched *symbol* string (used
    verbatim in the violation report), else ``None``.
    """

    def detect(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and node.id == symbol:
            return symbol
        if isinstance(node, ast.Attribute) and node.attr == symbol:
            return symbol
        return None

    return detect


def reference_in_set_detector(names: frozenset[str]) -> Detector:
    """A :data:`Detector` that flags a reference to any symbol in *names*.

    The name-set generalization of :func:`name_reference_detector`: matches a bare
    :class:`ast.Name` whose ``id`` is in *names* or an :class:`ast.Attribute` whose
    ``attr`` is in *names*, so a call, a plain reference, or a comparison operand over
    any member of the set is caught. Returns the matched name (used verbatim in a
    violation report), else ``None``. The salvage entry's disengage-site trigger — a
    reference to a ``Salvage`` verdict member.
    """

    def detect(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and node.id in names:
            return node.id
        if isinstance(node, ast.Attribute) and node.attr in names:
            return node.attr
        return None

    return detect


def call_in_set_detector(names: frozenset[str]) -> Detector:
    """A :data:`Detector` that flags a *call* whose callee simple-name is in *names*.

    Matches an :class:`ast.Call` whose callee is a bare :class:`ast.Name`
    (``f(...)`` -> ``"f"``) or an :class:`ast.Attribute` (``x.f(...)`` -> ``"f"``)
    whose simple name is in *names*; any other callee shape, and any non-call node,
    yields ``None``. A never-called reference (``fn = self.f`` — an ``Attribute``,
    not a ``Call``) does not match, so this is the give-up-primitive / oracle-consult
    trigger: only an actual invocation counts. Returns the matched callee name, else
    ``None``. Reproduces the retired bespoke ``_called_name`` folded with its
    membership test.
    """

    def detect(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if isinstance(func, ast.Name) and func.id in names:
            return func.id
        if isinstance(func, ast.Attribute) and func.attr in names:
            return func.attr
        return None

    return detect


def reset_hard_literal_detector(literal: str) -> Detector:
    """A :data:`Detector` that flags a ``reset_hard(...)`` call carrying *literal*.

    Matches an :class:`ast.Call` whose callee is ``reset_hard`` — as a bare
    :class:`ast.Name` (``reset_hard(...)``) or an :class:`ast.Attribute`
    (``self._vcs.reset_hard(...)``) — with a constant-string argument equal to
    *literal*. Reproduces the retired file's ``_reset_hard_targets`` string-arg
    collection folded into a per-node detector. Returns ``reset_hard("<literal>")``
    on a match, else ``None``.
    """

    def detect(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        is_reset = (isinstance(func, ast.Attribute) and func.attr == "reset_hard") or (
            isinstance(func, ast.Name) and func.id == "reset_hard"
        )
        if not is_reset:
            return None
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value == literal:
                return f'reset_hard("{literal}")'
        return None

    return detect


def _function_node(
    tree: ast.AST, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the ``def`` / ``async def`` named *name* in *tree*, else ``None``.

    ``ast.walk`` descends into class bodies, so a method resolves like the retired
    ``_tend_node``; sibling functions are separate nodes, so a symbol referenced
    only in a sibling never counts as "inside" *name*.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def assert_scoped_reference(
    requirement: ScopedReference,
    root: Path = _MAESTRO_ROOT,
    sources: dict[str, str] | None = None,
) -> None:
    """Assert *requirement*'s detector holds inside its named function.

    Reads ``requirement.module`` under *root* (mirroring
    :func:`assert_required_presence`'s reader) — or, when *sources* is supplied,
    from that in-memory map keyed by the same relative module path, so unit tests
    can stay off disk. On the disk path a missing target module fails with the same
    curated ``no longer exists`` retirement message :func:`assert_required_presence`
    gives; the named function being absent fails loud too — both ``must_be_present``
    *agnostic*, mirroring the retired ``_tend_node``'s "not found" raise — so a
    renamed/deleted module or method points at the entry to retire. Walks the
    located function's subtree, applies ``requirement.detector`` to every node, and
    asserts at least one hit when ``must_be_present`` else zero hits, naming the
    function, the module, and the requirement's ``message``. The function-scoped
    counterpart to :func:`assert_required_presence`.
    """
    if sources is not None:
        source = sources[requirement.module]
    else:
        path = root / requirement.module
        assert path.exists(), (
            f"guard requirement {requirement.name}: target module {requirement.module} "
            "no longer exists — retire or update this entry"
        )
        source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=requirement.module)
    func = _function_node(tree, requirement.function)
    assert func is not None, (
        f"guard requirement {requirement.name}: function {requirement.function} not "
        f"found in {requirement.module} — retire or update this entry"
    )
    hits = [
        primitive
        for node in ast.walk(func)
        if (primitive := requirement.detector(node)) is not None
    ]
    if requirement.must_be_present:
        assert hits, (
            f"guard requirement {requirement.name}: the required reference is absent "
            f"from {requirement.function} in {requirement.module}.\n\n{requirement.message}"
        )
    else:
        assert not hits, (
            f"guard requirement {requirement.name}: a forbidden reference is present "
            f"in {requirement.function} of {requirement.module}: {hits}.\n\n"
            f"{requirement.message}"
        )


def assert_co_occurrence(
    requirement: CoOccurrenceRequirement,
    root: Path = _MAESTRO_ROOT,
    sources: dict[str, str] | None = None,
) -> None:
    """Assert every trigger scope in a module also matches the companion detector.

    Reads ``requirement.module`` under *root* (mirroring
    :func:`assert_scoped_reference`'s reader) — or, when *sources* is supplied, from
    that in-memory map keyed by the same relative module path, so unit tests can stay
    off disk. On the disk path a missing target module fails with the same curated
    ``no longer exists`` retirement message the sibling assertions give. Maps each line
    to its *outermost* enclosing function via :func:`line_to_function` (so a closure
    resolves to its enclosing method, exactly as the retired bespoke
    ``_line_to_function`` did), collects the scopes where ``trigger`` fires and the
    scopes where ``companion`` fires, then, in order:

    (c) every ``expected_scopes`` / ``allowlist`` name is a real ``def`` in the module
        — folding in the retired ``test_allowlist_and_sites_name_real_functions``
        staleness check so a renamed site fails loud rather than silently widening a
        hole;
    (a) when ``expected_scopes is not None``, the set of trigger scopes equals it — a
        new or vanished trigger scope forces a reviewed update;
    (b) each trigger scope not in ``allowlist`` also matches the companion in that same
        scope.

    The scope-level co-occurrence counterpart to :func:`assert_scoped_reference`.
    """
    if sources is not None:
        source = sources[requirement.module]
    else:
        path = root / requirement.module
        assert path.exists(), (
            f"guard requirement {requirement.name}: target module {requirement.module} "
            "no longer exists — retire or update this entry"
        )
        source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=requirement.module)
    line_to_func = line_to_function(tree)

    trigger_scopes: set[str] = set()
    companion_scopes: set[str] = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        scope = line_to_func.get(line, _MODULE_SCOPE)
        if requirement.trigger(node) is not None:
            trigger_scopes.add(scope)
        if requirement.companion(node) is not None:
            companion_scopes.add(scope)

    # (c) staleness: every declared scope name must be a live ``def`` (else a rename
    # silently widens the guard's blind spot). Runs first so a config error surfaces
    # before the substantive checks.
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    declared = set(requirement.allowlist)
    if requirement.expected_scopes is not None:
        declared |= set(requirement.expected_scopes)
    stale = sorted(name for name in declared if name not in defined)
    assert not stale, (
        f"guard requirement {requirement.name}: names functions that no longer exist "
        f"in {requirement.module} — remove or update them: {', '.join(stale)}"
    )

    # (a) the exact set of trigger scopes, when locked.
    if requirement.expected_scopes is not None:
        assert trigger_scopes == set(requirement.expected_scopes), (
            f"guard requirement {requirement.name}: the trigger fires in "
            f"{sorted(trigger_scopes)} of {requirement.module}, but the curated set is "
            f"{sorted(requirement.expected_scopes)} — a new or vanished trigger scope "
            f"forces a reviewed update.\n\n{requirement.message}"
        )

    # (b) every non-allowlisted trigger scope also matches the companion.
    violators = sorted(
        scope
        for scope in trigger_scopes
        if scope not in requirement.allowlist and scope not in companion_scopes
    )
    assert not violators, (
        f"guard requirement {requirement.name}: trigger scope(s) in "
        f"{requirement.module} that never match the companion: {violators}"
        f"\n\n{requirement.message}"
    )


def assert_partition_co_occurrence(
    requirement: PartitionCoOccurrence,
    root: Path = _MAESTRO_ROOT,
    sources: dict[str, str] | None = None,
) -> None:
    """Assert no scope decides the owned/external partition without the companion.

    Walks every scanned module — read from disk under *root* (each ``*.py`` minus the
    ``exclude_modules`` basenames), or, when *sources* is supplied, the in-memory map
    keyed by relative module path (its basename minus ``exclude_modules``), so unit
    tests can stay off disk. Maps each line to its *outermost* enclosing function via
    :func:`line_to_function` (mirroring :func:`assert_co_occurrence`), then, per module,
    collects each scope's *distinct* ``code_detector`` values and the scopes where
    ``companion`` fires. A scope *decides the partition* when it names ``pivot`` and at
    least ``min_distinct`` distinct codes — a lone reference or a single-code mention
    decides nothing and is never flagged. Any such deciding scope that is not
    ``exclude_modules``-excluded, not in ``allowlist``, and never matches the companion
    in that same scope is a violator.

    Folds in the staleness check :func:`assert_co_occurrence` runs (every ``allowlist``
    ``(module, function)`` names a live ``def`` in that module), with the same curated
    ``no longer exist`` message so a renamed site fails loud rather than silently
    widening the hole — and a liveness check: every ``allowlist`` entry must name a
    scope that still decides the partition without the companion, else it exempts
    nothing and is a dead blind spot. The tree-wide scope-aggregate counterpart to
    :func:`assert_co_occurrence`.
    """
    if sources is not None:
        modules: list[tuple[str, str]] = [
            (Path(rel).name, source)
            for rel, source in sorted(sources.items())
            if Path(rel).name not in requirement.exclude_modules
        ]
    else:
        modules = [
            (path.name, path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.py"))
            if path.name not in requirement.exclude_modules
        ]

    trees = [(base, ast.parse(source, filename=base)) for base, source in modules]

    # Staleness: every allowlisted ``(module, function)`` must name a live ``def`` in
    # that scanned module (else a rename silently widens the guard's blind spot). Runs
    # first so a config error surfaces before the substantive check.
    by_module: dict[str, set[str]] = {}
    for base, tree in trees:
        by_module.setdefault(base, set()).update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    stale = sorted(
        f"{module}:{func}"
        for module, func in requirement.allowlist
        if func not in by_module.get(module, set())
    )
    assert not stale, (
        f"guard requirement {requirement.name}: allowlist names functions that no "
        f"longer exist — remove or update them: {', '.join(stale)}"
    )

    violators: list[str] = []
    deciding: set[tuple[str, str]] = set()
    for base, tree in trees:
        line_to_func = line_to_function(tree)
        codes_by_scope: dict[str, set[str]] = {}
        first_line: dict[str, int] = {}
        companion_scopes: set[str] = set()
        for node in ast.walk(tree):
            line = getattr(node, "lineno", None)
            if line is None:
                continue
            scope = line_to_func.get(line, _MODULE_SCOPE)
            code = requirement.code_detector(node)
            if code is not None:
                codes_by_scope.setdefault(scope, set()).add(code)
                first_line[scope] = min(first_line.get(scope, line), line)
            if requirement.companion(node) is not None:
                companion_scopes.add(scope)
        for scope, codes in codes_by_scope.items():
            if requirement.pivot not in codes or len(codes) < requirement.min_distinct:
                continue
            if scope in companion_scopes:
                continue
            deciding.add((base, scope))
            if (base, scope) in requirement.allowlist:
                continue
            violators.append(
                f"{base}:{first_line[scope]} — {sorted(codes)} grouped in {scope}"
            )

    dead = sorted(f"{module}:{func}" for module, func in requirement.allowlist - deciding)
    assert not dead, (
        f"guard requirement {requirement.name}: allowlist entry exempts no "
        f"partition-deciding scope — remove it: {', '.join(dead)}"
    )

    assert not violators, (
        f"guard requirement {requirement.name}: scope(s) deciding the partition "
        f"without consulting the companion:\n  " + "\n  ".join(sorted(violators))
        + f"\n\n{requirement.message}"
    )


def assert_module_purity(
    requirement: ModulePurity,
    root: Path = _MAESTRO_ROOT,
    source: str | None = None,
) -> None:
    """Assert *requirement.module* neither imports the forbidden prefix nor names a
    forbidden symbol.

    Reads ``requirement.module`` under *root* (mirroring :func:`assert_scoped_reference`'s
    reader) — or, when *source* is supplied, uses that string directly so unit tests can
    stay off disk. On the disk path a missing target module fails with the same curated
    ``no longer exists`` retirement message the sibling assertions give. Fails when the
    module imports the forbidden prefix or a submodule of it in any spelling, relative
    imports resolved against the module's package under *root* (via
    :func:`imports_from_prefix`) or references any ``forbidden_names`` member — a bare
    :class:`ast.Name` id or an :class:`ast.Attribute` attr, collected via
    :func:`reference_in_set_detector` over an ``ast.walk``. The module-scoped *forbidden*
    counterpart to :func:`assert_required_presence`'s required *present* import: the
    push-permission gate must consume its injected ``allowed_branches`` set and never
    re-derive ownership from the ``maestro.pr_facts`` fact home.
    """
    if source is not None:
        module_source = source
    else:
        path = root / requirement.module
        assert path.exists(), (
            f"guard requirement {requirement.name}: target module {requirement.module} "
            "no longer exists — retire or update this entry"
        )
        module_source = path.read_text(encoding="utf-8")
    tree = ast.parse(module_source, filename=requirement.module)
    package = ".".join([root.name, *Path(requirement.module).parent.parts])
    assert not imports_from_prefix(tree, requirement.forbidden_import_prefix, package), (
        f"guard requirement {requirement.name}: {requirement.module} imports from the "
        f"forbidden ``{requirement.forbidden_import_prefix}`` prefix — it must not "
        f"re-derive from that home.\n\n{requirement.message}"
    )
    detector = reference_in_set_detector(requirement.forbidden_names)
    referenced = sorted(
        {primitive for node in ast.walk(tree) if (primitive := detector(node)) is not None}
    )
    assert not referenced, (
        f"guard requirement {requirement.name}: {requirement.module} references forbidden "
        f"name(s) {referenced}.\n\n{requirement.message}"
    )
