"""Unit test for the reusable AST structural-guard harness (proposal 109).

Builds one synthetic :class:`ChokepointGuard` — a toy detector flagging calls to
a fabricated forbidden primitive, a ``synthetic_bypass`` that calls it, a
``synthetic_ok`` that does not — and exercises every harness operation over it.
Every assertion runs in memory or over an injected symbol set so the unit stays
under the default 5s timeout; the one case that reads the real ``maestro/`` tree
(the file-reading wrappers and default readers) is marked
``@pytest.mark.timeout(15)``, matching the bespoke chokepoint guards.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.structural_guard import (
    _MAESTRO_ROOT,
    ChokepointGuard,
    CoOccurrenceRequirement,
    ModulePurity,
    PartitionCoOccurrence,
    PresenceRequirement,
    ScopedReference,
    allowlisted_hits,
    assert_allowlist_entries_live,
    assert_allowlist_entries_real,
    assert_co_occurrence,
    assert_flags_synthetic,
    assert_module_purity,
    assert_partition_co_occurrence,
    assert_required_presence,
    assert_scoped_reference,
    assert_subjects_exist,
    assert_synthetic_ok,
    bound_symbol_names,
    call_in_set_detector,
    functions_by_module,
    imported_modules,
    line_to_function,
    line_to_innermost_function,
    name_reference_detector,
    reference_in_set_detector,
    reset_hard_literal_detector,
    scan_all_except,
    scan_all_except_paths,
    scan_for_violations,
    scan_paths,
    scan_source,
    symbols_defined_in,
)


def _toy_detector(node: ast.AST) -> str | None:
    """Flag a call to the fabricated ``forbidden_merge_call`` primitive.

    Matches both the bare ``forbidden_merge_call(...)`` and the attribute
    ``obj.forbidden_merge_call(...)`` call forms; every other node is clean.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    if name == "forbidden_merge_call":
        return "forbidden_merge_call()"
    return None


_TOY_GUARD = ChokepointGuard(
    name="toy",
    provenance="proposal 109 harness unit test",
    subjects=("is_pr_settled",),
    scanned=scan_paths("agent.py"),
    detector=_toy_detector,
    allowlist=frozenset({("probe.py", "blessed_caller")}),
    synthetic_bypass="def some_face():\n    return forbidden_merge_call()\n",
    synthetic_ok="def clean_face():\n    return other_call()\n",
)


def _exempt_line_2(tree: ast.AST) -> set[int]:
    """Toy ``exempt_lines`` hook: exempt only line 2 (the mirror of a blessed
    fallback-branch line the session-attachment guard would return)."""
    return {2}


def _exempt_line_99(tree: ast.AST) -> set[int]:
    """Toy ``exempt_lines`` hook: exempt a line no toy snippet occupies, so it is
    inert — used to prove the negative control still flags a real bypass."""
    return {99}


def test_line_to_function_maps_closure_to_outer() -> None:
    """A line inside a nested closure resolves to its outer named function."""
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return forbidden_merge_call()\n"
        "    return inner\n"
    )
    tree = ast.parse(source)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    mapping = line_to_function(tree)
    assert mapping[call.lineno] == "outer"


def test_line_to_innermost_function_maps_closure_to_itself() -> None:
    """A line inside a nested closure resolves to the closure, not its encloser.

    The mirror of :func:`test_line_to_function_maps_closure_to_outer`: the
    innermost resolver attributes the call to ``inner`` (the deepest enclosing
    function) rather than ``outer`` — the distinction the live-work guard needs so
    the async approval closures resolve to themselves.
    """
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return forbidden_merge_call()\n"
        "    return inner\n"
    )
    tree = ast.parse(source)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    mapping = line_to_innermost_function(tree)
    assert mapping[call.lineno] == "inner"


def test_scan_source_innermost_resolver_exempts_closure_not_encloser() -> None:
    """With the innermost resolver, an allowlisted closure is exempt but its
    non-allowlisted encloser is still flagged.

    The exact distinction the live-work guard relies on: a call inside the nested
    ``inner`` closure resolves to ``inner`` (allowlisted, suppressed), while a
    sibling call in the ``outer`` body resolves to ``outer`` (not allowlisted, so
    reported). Under the default outermost resolver both would resolve to
    ``outer`` and neither the exemption nor the flag would land on the right
    function.
    """
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return forbidden_merge_call()\n"
        "    forbidden_merge_call()\n"
        "    return inner\n"
    )
    violators = scan_source(
        source,
        _TOY_GUARD.detector,
        frozenset({("mod.py", "inner")}),
        "mod.py",
        line_resolver=line_to_innermost_function,
    )
    assert violators == ["mod.py:4 — forbidden_merge_call() in outer"]


def test_scan_source_exempts_line_from_exempt_lines_hook() -> None:
    """A violation whose line the ``exempt_lines`` hook returns is suppressed,
    while a sibling violation on a non-exempt line is still flagged.

    The per-line capability the session-attachment guard relies on: one blessed
    ``query()`` on an exempt line is tolerated while the rest of the function stays
    under guard. The ``(module, function)`` allowlist is empty, so only the hook
    can suppress line 2.
    """
    source = (
        "def face():\n"
        "    forbidden_merge_call()\n"  # line 2 — exempt via the hook
        "    forbidden_merge_call()\n"  # line 3 — still flagged
    )
    violators = scan_source(
        source,
        _TOY_GUARD.detector,
        frozenset(),
        "mod.py",
        exempt_lines=_exempt_line_2,
    )
    assert violators == ["mod.py:3 — forbidden_merge_call() in face"]


def test_scan_source_without_exempt_lines_flags_every_violation() -> None:
    """With ``exempt_lines`` absent (the default), every violation flags.

    Locks the behavior-preserving default the five pre-existing entries rely on:
    the same two-call snippet flags both lines when no hook is supplied.
    """
    source = (
        "def face():\n"
        "    forbidden_merge_call()\n"
        "    forbidden_merge_call()\n"
    )
    violators = scan_source(source, _TOY_GUARD.detector, frozenset(), "mod.py")
    assert violators == [
        "mod.py:2 — forbidden_merge_call() in face",
        "mod.py:3 — forbidden_merge_call() in face",
    ]


def test_assert_synthetic_ok_honors_exempt_lines_hook() -> None:
    """``assert_synthetic_ok`` suppresses a detector-positive node on an exempt
    line — the control threading the blessed fallback-branch call relies on.

    The same ``synthetic_ok`` over-fires without the hook, proving the exemption
    (not a silent detector change) is what keeps it clean.
    """
    guard = replace(
        _TOY_GUARD,
        synthetic_ok="def ok():\n    forbidden_merge_call()\n",
        exempt_lines=_exempt_line_2,
    )
    assert_synthetic_ok(guard)
    with pytest.raises(AssertionError, match="over-fires"):
        assert_synthetic_ok(replace(guard, exempt_lines=None))


def test_assert_flags_synthetic_flags_despite_exempt_lines_hook() -> None:
    """The negative control still flags a real bypass even with an ``exempt_lines``
    hook present — a bypass is never on a blessed line.

    The hook exempts line 99, which the bypass (whose call is on line 2) does not
    occupy, so the control is unaffected and still flags.
    """
    guard = replace(_TOY_GUARD, exempt_lines=_exempt_line_99)
    assert_flags_synthetic(guard)


def test_scan_source_flags_bypass_with_location() -> None:
    """``scan_source`` flags the bypass with ``file:line — primitive in func``."""
    violators = scan_source(
        _TOY_GUARD.synthetic_bypass, _TOY_GUARD.detector, frozenset(), "face.py"
    )
    assert violators == ["face.py:2 — forbidden_merge_call() in some_face"]


def test_scan_source_suppresses_allowlisted_function() -> None:
    """A call inside an allowlisted ``(module, func)`` pair is not reported."""
    source = "def blessed_caller():\n    return forbidden_merge_call()\n"
    violators = scan_source(
        source, _TOY_GUARD.detector, frozenset({("probe.py", "blessed_caller")}), "probe.py"
    )
    assert violators == []


def test_assert_flags_synthetic_passes_on_real_bypass() -> None:
    """The negative control passes when the bypass flags a violation."""
    assert_flags_synthetic(_TOY_GUARD)


def test_assert_flags_synthetic_fails_on_silent_detector() -> None:
    """The negative control fails when the bypass flags nothing (dead guard)."""
    dead = replace(_TOY_GUARD, synthetic_bypass="def x():\n    return ok()\n")
    with pytest.raises(AssertionError, match="silently dead"):
        assert_flags_synthetic(dead)


def test_assert_synthetic_ok_passes_on_clean_snippet() -> None:
    """The positive control passes when the OK snippet flags nothing."""
    assert_synthetic_ok(_TOY_GUARD)


def test_assert_synthetic_ok_fails_on_overfiring_detector() -> None:
    """The positive control fails when the OK snippet flags (over-firing)."""
    noisy = replace(
        _TOY_GUARD, synthetic_ok="def x():\n    return forbidden_merge_call()\n"
    )
    with pytest.raises(AssertionError, match="over-fires"):
        assert_synthetic_ok(noisy)


def test_assert_allowlist_entries_real_passes_with_live_entry() -> None:
    """A live allowlist entry passes against an injected ``by_module``."""
    live = {"probe.py": {"blessed_caller", "other"}}
    assert_allowlist_entries_real(_TOY_GUARD, by_module=live)


def test_assert_allowlist_entries_real_fails_on_stale_entry() -> None:
    """A stale allowlist entry (its function is gone) fails."""
    stale = {"probe.py": {"other"}}
    with pytest.raises(AssertionError, match="no longer exist"):
        assert_allowlist_entries_real(_TOY_GUARD, by_module=stale)


def test_module_scope_allowlist_agrees_across_scan_and_staleness() -> None:
    """A module-scope allowlist entry is honored by ``scan_source`` and accepted
    as non-stale by ``assert_allowlist_entries_real`` — the two paths agree on
    the ``<module>`` key even though ``functions_by_module`` never lists it.
    """
    source = "forbidden_merge_call()\n"  # a call at module scope, no enclosing def
    # Without an entry, the module-scope violation is reported against "<module>".
    assert scan_source(source, _TOY_GUARD.detector, frozenset(), "mod.py") == [
        "mod.py:1 — forbidden_merge_call() in <module>"
    ]
    # The module-scope allowlist entry suppresses it.
    allow = frozenset({("mod.py", "<module>")})
    assert scan_source(source, _TOY_GUARD.detector, allow, "mod.py") == []
    # And that same entry must not be reported stale, even against an empty
    # by_module (which, like functions_by_module, never contains "<module>").
    assert_allowlist_entries_real(replace(_TOY_GUARD, allowlist=allow), by_module={"mod.py": set()})


def test_assert_allowlist_entries_live_flags_entry_with_no_hit() -> None:
    """An allowlisted scope with no detector hit is a dead blind spot and fails."""
    sources = {"probe.py": "def blessed_caller():\n    return other_call()\n"}
    with pytest.raises(AssertionError, match="exempts no live hit") as exc:
        assert_allowlist_entries_live(_TOY_GUARD, sources=sources)
    assert "probe.py:blessed_caller" in str(exc.value)


def test_assert_allowlist_entries_live_passes_when_entry_shields_hit() -> None:
    """An allowlisted scope that still contains a hit is live (positive control)."""
    sources = {"probe.py": "def blessed_caller():\n    return forbidden_merge_call()\n"}
    assert allowlisted_hits(_TOY_GUARD, sources=sources) == {("probe.py", "blessed_caller")}
    assert_allowlist_entries_live(_TOY_GUARD, sources=sources)


def test_assert_allowlist_entries_live_counts_module_scope_hit() -> None:
    """A module-scope entry is live when a hit sits at module level."""
    guard = replace(_TOY_GUARD, allowlist=frozenset({("mod.py", "<module>")}))
    assert_allowlist_entries_live(guard, sources={"mod.py": "forbidden_merge_call()\n"})
    with pytest.raises(AssertionError, match=r"mod\.py:<module>"):
        assert_allowlist_entries_live(guard, sources={"mod.py": "other_call()\n"})


def test_assert_subjects_exist_passes_with_live_symbol() -> None:
    """A subject present in the injected defined-set passes."""
    assert_subjects_exist(_TOY_GUARD, defined={"is_pr_settled", "unrelated"})


def test_assert_subjects_exist_fails_on_deleted_subject() -> None:
    """A deleted subject fails with the retirement message."""
    with pytest.raises(AssertionError, match="no longer exists — retire this entry"):
        assert_subjects_exist(_TOY_GUARD, defined={"unrelated"})


def test_assert_subjects_exist_resolves_module_level_constant() -> None:
    """A subject naming a module-level constant (not a def/class) resolves.

    Locks the fix for the retirement trigger firing falsely on registry lists /
    sentinel constants: a defined-set built from a snippet with module-level
    assignments contains the assigned names, so a subject naming one passes.
    """
    snippet = "GATE_CHAIN = [1, 2, 3]\nSENTINEL: str = 'x'\nA, B = 1, 2\n"
    defined = bound_symbol_names(ast.parse(snippet))
    assert {"GATE_CHAIN", "SENTINEL", "A", "B"} <= defined
    for subject in ("GATE_CHAIN", "SENTINEL", "A"):
        assert_subjects_exist(replace(_TOY_GUARD, subjects=(subject,)), defined=defined)


def test_bound_symbol_names_excludes_function_local_assignment() -> None:
    """Function-local assignments are not defined subjects (no false pass).

    A class-level attribute stays eligible (class body is not a function scope),
    but a name bound only inside a function must not mask a deleted subject.
    """
    snippet = (
        "TOP = 1\n"
        "class C:\n"
        "    ATTR = 2\n"
        "    def m(self):\n"
        "        INNER = 3\n"
        "        return INNER\n"
    )
    defined = bound_symbol_names(ast.parse(snippet))
    assert {"TOP", "ATTR", "C", "m"} <= defined
    assert "INNER" not in defined


_TOY_PRESENCE = PresenceRequirement(
    name="toy_presence",
    targets=("a.py", "b.py"),
    import_module="maestro.providers.base",
    import_name="Session",
    message="each module must hold the opaque Session handle",
)


def test_assert_required_presence_passes_when_all_targets_import() -> None:
    """Every target module carrying the required import → no failure.

    Drives ``assert_required_presence`` over an injected in-memory source map so the
    unit stays off disk. An unrelated ``import`` and a multi-name ``ImportFrom``
    alongside the required one prove the predicate keeps scanning past non-matching
    nodes to find the ``from maestro.providers.base import Session`` presence.
    """
    sources = {
        "a.py": "import os\nfrom maestro.providers.base import Session\n",
        "b.py": "from maestro.providers.base import QueryResult, Session\n",
    }
    assert_required_presence(_TOY_PRESENCE, sources=sources)


def test_assert_required_presence_fails_naming_missing_module() -> None:
    """A target missing the required import fails, naming that module and the import.

    ``b.py`` imports ``Session`` from the *wrong* module and imports the *wrong*
    name from the right module, so :func:`_imports_from` returns ``False`` for it
    while ``a.py`` (which has the real import) passes. The assertion names the
    required import and the single missing module, and does not name the module
    that has it.
    """
    sources = {
        "a.py": "from maestro.providers.base import Session\n",
        "b.py": "from other.module import Session\nfrom maestro.providers.base import QueryResult\n",
    }
    with pytest.raises(AssertionError, match="modules missing") as excinfo:
        assert_required_presence(_TOY_PRESENCE, sources=sources)
    message = str(excinfo.value)
    assert "from maestro.providers.base import Session" in message
    assert "b.py" in message
    assert "a.py" not in message


def test_assert_required_presence_reports_missing_target_module(
    mem_fs: dict[str, str],
) -> None:
    """A target that no longer exists on disk fails with a curated retirement message.

    Regression for the raw ``FileNotFoundError`` a renamed/deleted consumer module
    used to raise on the disk-read path: the read now asserts the target exists
    first, mirroring :func:`assert_subjects_exist`'s ``no longer exists`` retirement
    message so guard maintenance sees *which* entry to update. ``a.py`` is present in
    the in-memory tree and ``b.py`` is absent, so the assertion names the missing
    ``b.py`` and nothing about the present ``a.py``.
    """
    root = Path("/fake/guarded")
    mem_fs[str(root / "a.py")] = "from maestro.providers.base import Session\n"
    with pytest.raises(AssertionError, match="no longer exists") as excinfo:
        assert_required_presence(_TOY_PRESENCE, root=root)
    message = str(excinfo.value)
    assert "b.py" in message
    assert "a.py" not in message


@pytest.mark.timeout(15)
def test_real_tree_readers_and_wrappers_resolve() -> None:
    """The file-reading wrappers and default readers resolve against ``maestro/``.

    Collapses every real-tree-I/O path into one component-budget case: the
    file-reading scan wrapper, both scan-scope resolvers, and the symbol readers
    behind ``assert_subjects_exist`` / ``assert_allowlist_entries_real``.
    """
    # File-reading wrapper: the toy primitive never appears in real ``agent.py``.
    assert scan_for_violations(_TOY_GUARD) == []

    # ``scan_all_except`` excludes named owners and returns sorted paths.
    everything = scan_all_except("agent.py")(_MAESTRO_ROOT)
    names = [p.name for p in everything]
    assert "agent.py" not in names
    assert everything == sorted(everything)

    # ``scan_all_except_paths`` excludes an exact file and a whole subtree while a
    # basename collision under a different path survives — the path-keyed distinction
    # the vendor-SDK-import guard needs (``base.py`` lives under both ``providers/``
    # and ``notifications/``). Returns sorted output like ``scan_all_except``.
    path_scoped = scan_all_except_paths("sdk.py", "providers/")(_MAESTRO_ROOT)
    rels = [p.relative_to(_MAESTRO_ROOT).as_posix() for p in path_scoped]
    assert "sdk.py" not in rels
    assert not any(rel.startswith("providers/") for rel in rels)
    assert "providers/base.py" not in rels
    assert "notifications/base.py" in rels
    assert path_scoped == sorted(path_scoped)

    # ``scan_paths`` yields exactly the requested file under the real root.
    assert scan_paths("agent.py")(_MAESTRO_ROOT) == [_MAESTRO_ROOT / "agent.py"]

    # Direct symbol readers over the real tree.
    assert "run" in functions_by_module([_MAESTRO_ROOT / "agent.py"])["agent.py"]
    assert "is_pr_settled" in symbols_defined_in(_MAESTRO_ROOT)

    # Default readers behind the assertions resolve against ``maestro/``.
    assert_subjects_exist(_TOY_GUARD)
    assert_allowlist_entries_real(replace(_TOY_GUARD, allowlist=frozenset({("agent.py", "run")})))


# ── Function-scoped reference requirement (proposal 109) ────────────────────────
#
# The scoped-reference capability that let the origin-grounded entry drop its
# bespoke twin: assert a detector matches (or does not match) *inside one named
# function* of one module. Mirrors the ``assert_required_presence`` trio and adds
# direct coverage of the two detector factories the origin-grounded entry reuses.


def _expr(source: str) -> ast.expr:
    """The single expression node parsed from a one-line snippet."""
    stmt = ast.parse(source).body[0]
    assert isinstance(stmt, ast.Expr)
    return stmt.value


# A synthetic module whose ``target`` method consults the grounded observer and
# heals with ``reset_hard("origin/main")`` but never names the host proxy, while a
# ``sibling`` method *does* name the host proxy — so a symbol referenced only in the
# sibling must not count as "inside" ``target``.
_SCOPED_MODULE = (
    "class E:\n"
    "    def target(self, branch):\n"
    "        staleness = self._observe_pr_staleness(branch)\n"
    "        self._vcs.reset_hard('origin/main')\n"
    "        return staleness\n"
    "    def sibling(self, x):\n"
    "        return self._pr_needs_conflict_resolution(x)\n"
)

_OBSERVER_REQ = ScopedReference(
    name="observer_present",
    module="evolution.py",
    function="target",
    detector=name_reference_detector("_observe_pr_staleness"),
    must_be_present=True,
    message="the grounded observer must be consulted inside the target function",
)


def test_name_reference_detector_matches_name_and_attribute_shapes() -> None:
    """The name detector flags a bare name and an attribute, ignoring the rest.

    Reproduces the ``_names_in`` coverage the retired file carried: a bare
    ``ast.Name`` (``_observe_pr_staleness``) and an ``ast.Attribute`` attr
    (``self._observe_pr_staleness``) both match, while an unrelated name, an
    attribute with a different attr, and a non-name/attribute node all return
    ``None``.
    """
    detect = name_reference_detector("_observe_pr_staleness")
    assert detect(_expr("_observe_pr_staleness")) == "_observe_pr_staleness"
    assert detect(_expr("self._observe_pr_staleness")) == "_observe_pr_staleness"
    assert detect(_expr("something_else")) is None
    assert detect(_expr("self.other_attr")) is None
    assert detect(_expr("42")) is None


def test_reset_hard_literal_detector_matches_literal_arg_only() -> None:
    """The reset-hard detector flags only a ``reset_hard`` call carrying the literal.

    Reproduces the ``_reset_hard_targets`` coverage: the attribute form
    (``self._vcs.reset_hard('origin/main')``) and the bare-name form
    (``reset_hard('origin/main')``) both match, while a different string arg, a
    non-``reset_hard`` call, and a non-call node all return ``None``.
    """
    detect = reset_hard_literal_detector("origin/main")
    assert detect(_expr("self._vcs.reset_hard('origin/main')")) == 'reset_hard("origin/main")'
    assert detect(_expr("reset_hard('origin/main')")) == 'reset_hard("origin/main")'
    assert detect(_expr("self._vcs.reset_hard('origin/feature')")) is None
    assert detect(_expr("self._vcs.checkout('origin/main')")) is None
    assert detect(_expr("'origin/main'")) is None


def test_assert_scoped_reference_present_required_matches_passes() -> None:
    """A present-required detector that matches inside the function passes.

    Drives ``assert_scoped_reference`` over an injected in-memory source map so the
    unit stays off disk: the observer is consulted inside ``target``.
    """
    assert_scoped_reference(_OBSERVER_REQ, sources={"evolution.py": _SCOPED_MODULE})


def test_assert_scoped_reference_present_required_missing_fails() -> None:
    """A present-required detector matching only a *sibling* function fails.

    ``_pr_needs_conflict_resolution`` is referenced in ``sibling`` but not
    ``target``, so scoping to ``target`` finds no hit — proving the scan is
    function-scoped, not whole-module. The message names the absent reference.
    """
    req = replace(
        _OBSERVER_REQ,
        name="sibling_only",
        detector=name_reference_detector("_pr_needs_conflict_resolution"),
    )
    with pytest.raises(AssertionError, match="required reference is absent"):
        assert_scoped_reference(req, sources={"evolution.py": _SCOPED_MODULE})


def test_assert_scoped_reference_absent_required_present_fails() -> None:
    """An absent-required detector that *does* match inside the function fails.

    The observer is present in ``target``, so a ``must_be_present=False``
    requirement for it flags a forbidden reference.
    """
    req = replace(_OBSERVER_REQ, name="observer_forbidden", must_be_present=False)
    with pytest.raises(AssertionError, match="forbidden reference is present"):
        assert_scoped_reference(req, sources={"evolution.py": _SCOPED_MODULE})


def test_assert_scoped_reference_absent_required_missing_passes() -> None:
    """An absent-required detector that matches nowhere in the function passes.

    The host proxy ``_pr_needs_conflict_resolution`` is not referenced inside
    ``target`` (only in ``sibling``), so a ``must_be_present=False`` requirement
    for it holds — the origin-grounded entry's host-proxy-absent check.
    """
    req = ScopedReference(
        name="host_proxy_absent",
        module="evolution.py",
        function="target",
        detector=name_reference_detector("_pr_needs_conflict_resolution"),
        must_be_present=False,
        message="the host proxy must not appear inside the target function",
    )
    assert_scoped_reference(req, sources={"evolution.py": _SCOPED_MODULE})


def test_assert_scoped_reference_reset_hard_literal_present_passes() -> None:
    """A present-required ``reset_hard`` literal detector matches inside the function.

    Exercises the reset-hard detector factory end-to-end through the scoped
    assertion — the origin-grounded entry's heal-reset check.
    """
    req = ScopedReference(
        name="heal_reset_present",
        module="evolution.py",
        function="target",
        detector=reset_hard_literal_detector("origin/main"),
        must_be_present=True,
        message="the local-main heal reset must be present",
    )
    assert_scoped_reference(req, sources={"evolution.py": _SCOPED_MODULE})


@pytest.mark.parametrize("must_be_present", [True, False])
def test_assert_scoped_reference_missing_function_fails_loud(must_be_present: bool) -> None:
    """A requirement whose named function is absent fails loud, either polarity.

    ``must_be_present``-agnostic, mirroring the retired ``_tend_node``'s "not
    found" raise: a renamed/deleted method points at the entry to retire rather
    than silently passing (an absent function has no hits, which would otherwise
    satisfy a ``must_be_present=False`` requirement).
    """
    req = replace(_OBSERVER_REQ, function="does_not_exist", must_be_present=must_be_present)
    with pytest.raises(AssertionError, match="not found in"):
        assert_scoped_reference(req, sources={"evolution.py": _SCOPED_MODULE})


def test_assert_scoped_reference_reads_module_from_disk(mem_fs: dict[str, str]) -> None:
    """With no ``sources`` map, the module is read from disk under ``root``.

    Covers the disk-read path (the default reader) via the in-memory ``mem_fs``
    tree, mirroring :func:`assert_required_presence`'s disk path.
    """
    root = Path("/fake/guarded")
    mem_fs[str(root / "evolution.py")] = _SCOPED_MODULE
    assert_scoped_reference(_OBSERVER_REQ, root=root)


def test_assert_scoped_reference_reports_missing_target_module(
    mem_fs: dict[str, str],
) -> None:
    """A target module absent on disk fails with the curated retirement message.

    Regression against a raw ``FileNotFoundError`` on the disk-read path: the read
    asserts the target exists first, mirroring :func:`assert_subjects_exist`'s
    ``no longer exists`` retirement message. ``evolution.py`` is absent from the
    in-memory tree, so the assertion names it.
    """
    root = Path("/fake/guarded")
    with pytest.raises(AssertionError, match="no longer exists") as excinfo:
        assert_scoped_reference(_OBSERVER_REQ, root=root)
    assert "evolution.py" in str(excinfo.value)


# ── Scope-level co-occurrence requirement (proposal 109) ────────────────────────
#
# The co-occurrence capability that lets the salvage entry drop its bespoke twin:
# assert that, within one module, every function where a *trigger* detector fires
# also matches a *companion* detector (optionally locking the exact trigger-scope
# set). Mirrors the ``assert_scoped_reference`` trio and adds direct coverage of the
# two set-membership detector factories the entry reuses.


# The two set-membership detectors the salvage entry wires: a reference to a
# ``Salvage`` verdict member (the disengage-site trigger), a call to the oracle
# gatherer (the companion), and a call to a durable give-up primitive (the
# stand-down-writer trigger).
_VERDICT_TRIGGER = reference_in_set_detector(
    frozenset({"SALVAGE_AND_LAND", "DISENGAGE_PERMITTED"})
)
_ORACLE_COMPANION = call_in_set_detector(
    frozenset({"_assess_pr_salvage", "assess_pr_salvage"})
)
_GIVE_UP_TRIGGER = call_in_set_detector(
    frozenset({"record_stand_down", "mechanical_standdown_verdict"})
)

# A synthetic module modeling the salvage shape:
#   alpha   — references a verdict, consults the oracle, and writes a stand-down
#             (a clean disengage site + clean give-up author);
#   beta    — references a verdict and consults the oracle (clean, no stand-down);
#   builder — calls a give-up primitive but never consults the oracle (the
#             ``_mechanical_standdown_verdict``-style build-only author to allowlist);
#   unrelated — nothing fires.
_CO_MODULE = (
    "class A:\n"
    "    def alpha(self, pr, branch):\n"
    "        verdict = self._assess_pr_salvage(pr, branch)\n"
    "        if verdict is Salvage.SALVAGE_AND_LAND:\n"
    "            return False\n"
    "        record_stand_down(pr, branch)\n"
    "        return True\n"
    "    def beta(self, pr, branch):\n"
    "        v = self.assess_pr_salvage(pr, branch)\n"
    "        return v is Salvage.DISENGAGE_PERMITTED\n"
    "    def builder(self, entry):\n"
    "        return mechanical_standdown_verdict(entry)\n"
    "    def unrelated(self, x):\n"
    "        return x + 1\n"
)

# The disengage-site requirement: every verdict-referencing scope must consult the
# oracle, with the exact trigger-scope set locked.
_DISENGAGE_REQ = CoOccurrenceRequirement(
    name="disengage_consults_oracle",
    module="agent.py",
    trigger=_VERDICT_TRIGGER,
    companion=_ORACLE_COMPANION,
    expected_scopes=frozenset({"alpha", "beta"}),
    allowlist=frozenset(),
    message="each disengage site must consult the salvage oracle",
)

# The stand-down-writer requirement: every give-up author must consult the oracle,
# unless whole-function-allowlisted; the trigger-scope set is not locked.
_STANDDOWN_REQ = CoOccurrenceRequirement(
    name="standdown_consults_oracle",
    module="agent.py",
    trigger=_GIVE_UP_TRIGGER,
    companion=_ORACLE_COMPANION,
    expected_scopes=None,
    allowlist=frozenset({"builder"}),
    message="each give-up author must consult the salvage oracle",
)


def test_reference_in_set_detector_matches_name_and_attribute_shapes() -> None:
    """The set-membership reference detector flags bare names and attributes only.

    Generalizes ``name_reference_detector`` to a name set: a bare ``ast.Name`` and an
    ``ast.Attribute`` attr in the set both match and return the matched member, while
    an unrelated name, an attribute with a different attr, and a non-name/attribute
    node all return ``None``.
    """
    detect = reference_in_set_detector(
        frozenset({"SALVAGE_AND_LAND", "DISENGAGE_PERMITTED"})
    )
    assert detect(_expr("SALVAGE_AND_LAND")) == "SALVAGE_AND_LAND"
    assert detect(_expr("Salvage.DISENGAGE_PERMITTED")) == "DISENGAGE_PERMITTED"
    assert detect(_expr("something_else")) is None
    assert detect(_expr("obj.other_attr")) is None
    assert detect(_expr("42")) is None


def test_call_in_set_detector_matches_call_by_callee_name_only() -> None:
    """The set-membership call detector flags a call by callee name, not a reference.

    Matches an ``ast.Call`` whose bare-``Name`` or ``Attribute`` callee simple-name is
    in the set and returns it, while a never-called reference (an ``Attribute`` /
    bare ``Name``, not a ``Call``), an unrelated call, and a non-call node all return
    ``None`` — proving only an actual invocation counts.
    """
    detect = call_in_set_detector(frozenset({"record_stand_down", "assess_pr_salvage"}))
    assert detect(_expr("record_stand_down(pr)")) == "record_stand_down"
    assert detect(_expr("self.assess_pr_salvage(pr, branch)")) == "assess_pr_salvage"
    assert detect(_expr("self.record_stand_down")) is None
    assert detect(_expr("record_stand_down")) is None
    assert detect(_expr("self.other_call(pr)")) is None
    assert detect(_expr("42")) is None


def test_assert_co_occurrence_companion_present_passes() -> None:
    """A requirement whose every trigger scope also matches the companion passes.

    Drives ``assert_co_occurrence`` over an injected in-memory source map: ``alpha``
    and ``beta`` both reference a verdict *and* consult the oracle, and the locked
    ``expected_scopes`` matches the trigger set exactly.
    """
    assert_co_occurrence(_DISENGAGE_REQ, sources={"agent.py": _CO_MODULE})


def test_assert_co_occurrence_allowlisted_scope_without_companion_passes() -> None:
    """An allowlisted trigger scope that never matches the companion still passes.

    ``builder`` calls a give-up primitive but never consults the oracle; because it
    is whole-function-allowlisted, the requirement holds — the ``_mechanical_standdown_
    verdict`` build-only exemption.
    """
    assert_co_occurrence(_STANDDOWN_REQ, sources={"agent.py": _CO_MODULE})


def test_assert_co_occurrence_companion_absent_scope_fails_naming_it() -> None:
    """A trigger scope that never matches the companion fails, naming that scope.

    Dropping ``builder`` from the allowlist leaves a give-up author that never
    consults the oracle — the "new actuator caught" check the bespoke file made.
    """
    req = replace(_STANDDOWN_REQ, allowlist=frozenset())
    with pytest.raises(AssertionError, match="never match the companion") as excinfo:
        assert_co_occurrence(req, sources={"agent.py": _CO_MODULE})
    assert "builder" in str(excinfo.value)


def test_assert_co_occurrence_expected_scopes_mismatch_fails() -> None:
    """A locked ``expected_scopes`` that misses or gains a trigger scope fails loud.

    Both directions bite the same equality check, with every named scope a real
    ``def`` so the staleness check passes first: a *missing* trigger scope (``beta``
    still fires) and an *extra* real scope declared (``unrelated`` never fires).
    """
    missing = replace(_DISENGAGE_REQ, expected_scopes=frozenset({"alpha"}))
    with pytest.raises(AssertionError, match="forces a reviewed update"):
        assert_co_occurrence(missing, sources={"agent.py": _CO_MODULE})

    extra = replace(
        _DISENGAGE_REQ, expected_scopes=frozenset({"alpha", "beta", "unrelated"})
    )
    with pytest.raises(AssertionError, match="forces a reviewed update"):
        assert_co_occurrence(extra, sources={"agent.py": _CO_MODULE})


def test_assert_co_occurrence_stale_declared_name_fails_loud() -> None:
    """A stale ``expected_scopes`` or ``allowlist`` name fails loud (staleness check).

    Folds in the retired ``test_allowlist_and_sites_name_real_functions``: a declared
    scope naming no live ``def`` — in either ``expected_scopes`` or ``allowlist`` —
    fails before the substantive checks so a rename can't silently widen the hole.
    """
    stale_expected = replace(
        _DISENGAGE_REQ, expected_scopes=frozenset({"alpha", "beta", "ghost"})
    )
    with pytest.raises(AssertionError, match="remove or update them") as excinfo:
        assert_co_occurrence(stale_expected, sources={"agent.py": _CO_MODULE})
    assert "ghost" in str(excinfo.value)

    stale_allowlist = replace(_STANDDOWN_REQ, allowlist=frozenset({"ghost"}))
    with pytest.raises(AssertionError, match="remove or update them"):
        assert_co_occurrence(stale_allowlist, sources={"agent.py": _CO_MODULE})


def test_assert_co_occurrence_reads_module_from_disk(mem_fs: dict[str, str]) -> None:
    """With no ``sources`` map, the module is read from disk under ``root``.

    Covers the disk-read path (the default reader) via the in-memory ``mem_fs`` tree,
    mirroring :func:`assert_scoped_reference`'s disk path.
    """
    root = Path("/fake/guarded")
    mem_fs[str(root / "agent.py")] = _CO_MODULE
    assert_co_occurrence(_DISENGAGE_REQ, root=root)


def test_assert_co_occurrence_reports_missing_target_module(
    mem_fs: dict[str, str],
) -> None:
    """A target module absent on disk fails with the curated retirement message.

    Regression against a raw ``FileNotFoundError`` on the disk-read path: the read
    asserts the target exists first, mirroring :func:`assert_subjects_exist`'s
    ``no longer exists`` retirement message. ``agent.py`` is absent from the in-memory
    tree, so the assertion names it.
    """
    root = Path("/fake/guarded")
    with pytest.raises(AssertionError, match="no longer exists") as excinfo:
        assert_co_occurrence(_DISENGAGE_REQ, root=root)
    assert "agent.py" in str(excinfo.value)


# ── Scope-aggregate partition co-occurrence requirement (proposal 109) ──────────
#
# The partition-co-occurrence capability that lets the blocker-ownership entry drop
# its bespoke twin's ``test_no_consumer_re_derives_blocker_ownership`` scan: a
# tree-wide rule that no scope naming the pivot code plus another distinct code
# decides the owned/external partition without consulting the classifier. Unlike the
# single-node detectors, its trigger is a *scope aggregate* ("names the pivot plus at
# least one other distinct code"). Mirrors the ``assert_co_occurrence`` trio — a
# consulting scope passes, a non-consulting partition scope flags naming it, a
# below-threshold reference does not, an allowlisted scope passes, a stale allowlist
# entry fails loud, and an ``exclude_modules`` member is never scanned.

# A toy reason-code taxonomy: constant name -> value, so the detector recognizes both
# the bare-constant (``MERGE_BLOCK_AWAITING_PUSH``) and string-literal (``"AWAITING_PUSH"``)
# spellings, exactly as the blocker-ownership entry's real detector does.
_TOY_REASON_CONSTANTS = {
    "MERGE_BLOCK_AWAITING_PUSH": "AWAITING_PUSH",
    "MERGE_BLOCK_AWAITING_APPROVAL": "AWAITING_APPROVAL",
    "MERGE_BLOCK_BEHIND_MAIN": "BEHIND_MAIN",
}
_TOY_REASON_VALUES = frozenset(_TOY_REASON_CONSTANTS.values())
_TOY_PIVOT = "AWAITING_PUSH"


def _toy_code_detector(node: ast.AST) -> str | None:
    """Return the toy reason-code *value* *node* names, else ``None``.

    Recognizes a string ``Constant`` in the taxonomy, an imported constant
    ``ast.Name``, and a qualified ``ast.Attribute`` — the three spellings a consumer
    can use, mirroring the real ``_blocker_code_detector``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in _TOY_REASON_VALUES else None
    if isinstance(node, ast.Name):
        return _TOY_REASON_CONSTANTS.get(node.id)
    if isinstance(node, ast.Attribute):
        return _TOY_REASON_CONSTANTS.get(node.attr)
    return None


_TOY_COMPANION = reference_in_set_detector(frozenset({"classify_blocker_ownership"}))

# A scope that groups the pivot with another code but never consults the classifier —
# a re-derived partition the guard must flag.
_PARTITION_BYPASS = (
    "def some_gate(reason):\n"
    "    tend = (MERGE_BLOCK_AWAITING_PUSH, MERGE_BLOCK_AWAITING_APPROVAL)\n"
    "    return reason in tend\n"
)
# The sanctioned shape: the same grouping, but the classifier is consulted in-scope.
_PARTITION_OK = (
    "def sanctioned(reason):\n"
    "    owned = classify_blocker_ownership(MERGE_BLOCK_AWAITING_PUSH)\n"
    "    tend = (MERGE_BLOCK_AWAITING_PUSH, MERGE_BLOCK_AWAITING_APPROVAL)\n"
    "    return owned and reason in tend\n"
)
# Two scopes below the distinct-code floor: a lone pivot comparison and a single-code
# container — neither decides the partition, so neither flags.
_PARTITION_BELOW = (
    "def lone(reason):\n"
    "    return reason == MERGE_BLOCK_AWAITING_PUSH\n"
    "\n"
    "def single(reason):\n"
    "    return reason in (MERGE_BLOCK_AWAITING_PUSH,)\n"
)


def _partition_req(
    allowlist: frozenset[tuple[str, str]] = frozenset(),
    exclude_modules: frozenset[str] = frozenset(),
) -> PartitionCoOccurrence:
    """A toy partition requirement with the given allowlist / exclusions."""
    return PartitionCoOccurrence(
        name="toy_partition",
        code_detector=_toy_code_detector,
        pivot=_TOY_PIVOT,
        companion=_TOY_COMPANION,
        allowlist=allowlist,
        exclude_modules=exclude_modules,
        message="every consumer must consult the classifier",
    )


def test_assert_partition_co_occurrence_non_consulting_scope_flags_naming_it() -> None:
    """A scope grouping the pivot with another code, never consulting, flags naming it.

    Drives ``assert_partition_co_occurrence`` over an injected in-memory source map:
    ``some_gate`` names the pivot plus a second code without the companion, the
    scope-aggregate re-derivation the bespoke ``test_no_consumer_re_derives_blocker_
    ownership`` caught.
    """
    with pytest.raises(AssertionError, match="without consulting the companion") as exc:
        assert_partition_co_occurrence(
            _partition_req(), sources={"consumer.py": _PARTITION_BYPASS}
        )
    assert "some_gate" in str(exc.value)


def test_assert_partition_co_occurrence_consulting_scope_passes() -> None:
    """A partition scope that consults the companion in-scope passes.

    ``sanctioned`` groups the same two codes but calls the classifier in the same
    scope, so the grouping is licensed — the codes are the classifier's input, not a
    substitute for it.
    """
    assert_partition_co_occurrence(
        _partition_req(), sources={"consumer.py": _PARTITION_OK}
    )


def test_assert_partition_co_occurrence_below_min_distinct_does_not_flag() -> None:
    """A lone reference and a single-code container never decide the partition.

    Both ``lone`` (a bare ``==`` against the pivot) and ``single`` (a one-code
    container) name only the pivot — below the ``min_distinct`` floor of 2 — so
    neither is a grouping and neither flags.
    """
    assert_partition_co_occurrence(
        _partition_req(), sources={"consumer.py": _PARTITION_BELOW}
    )


def test_assert_partition_co_occurrence_allowlisted_scope_passes() -> None:
    """An allowlisted ``(module, function)`` partition scope passes despite no companion.

    ``some_gate`` re-derives the partition, but the ``(consumer.py, some_gate)``
    exemption licenses it — the producer-of-codes escape hatch.
    """
    assert_partition_co_occurrence(
        _partition_req(allowlist=frozenset({("consumer.py", "some_gate")})),
        sources={"consumer.py": _PARTITION_BYPASS},
    )


def test_assert_partition_co_occurrence_stale_allowlist_fails_loud() -> None:
    """A stale allowlist entry naming no live ``def`` fails loud (staleness check).

    Folds in the retired ``test_allowlist_entries_name_real_functions``: a
    ``(module, function)`` naming no ``def`` in that scanned module fails before the
    substantive scan so a rename cannot silently widen the hole.
    """
    with pytest.raises(AssertionError, match="remove or update them") as exc:
        assert_partition_co_occurrence(
            _partition_req(allowlist=frozenset({("consumer.py", "ghost")})),
            sources={"consumer.py": _PARTITION_BYPASS},
        )
    assert "ghost" in str(exc.value)


def test_assert_partition_co_occurrence_dead_allowlist_entry_fails_loud() -> None:
    """An allowlisted ``def`` that no longer decides the partition fails loud.

    ``sanctioned`` exists but consults the companion, and ``lone``/``single`` name
    only the pivot — none is a deciding, non-consulting scope, so an entry for any of
    them exempts nothing and must be deleted.
    """
    for source, func in (
        (_PARTITION_OK, "sanctioned"),
        (_PARTITION_BELOW, "lone"),
        (_PARTITION_BELOW, "single"),
    ):
        with pytest.raises(AssertionError, match="exempts no partition-deciding scope") as exc:
            assert_partition_co_occurrence(
                _partition_req(allowlist=frozenset({("consumer.py", func)})),
                sources={"consumer.py": source},
            )
        assert func in str(exc.value)


def test_assert_partition_co_occurrence_excluded_module_is_never_scanned() -> None:
    """A re-derivation inside an ``exclude_modules`` member is never flagged.

    The taxonomy's own source module is entitled to enumerate the codes because it
    *defines* them, so a partition scope there — which every other module's scope would
    trip on — is excluded from the scan entirely.
    """
    assert_partition_co_occurrence(
        _partition_req(exclude_modules=frozenset({"remediation_tracker.py"})),
        sources={"remediation_tracker.py": _PARTITION_BYPASS},
    )


# The default disk-read path (``root.rglob("*.py")`` + ``read_text``) is exercised over
# the real ``maestro/`` tree by ``test_partition_co_occurrences_hold`` in
# ``tests/test_structural_guards.py`` — the ``mem_fs`` fixture does not patch ``rglob``,
# so an in-memory disk test would scan nothing and pass vacuously.


# ── Module-scoped purity requirement (proposal 109) ─────────────────────────────
#
# The module-purity capability that lets the push-permission entry drop its bespoke
# twin's ``test_gate_module_never_re_derives_ownership`` check: assert one module
# neither imports a forbidden dotted prefix nor references any forbidden name. Mirrors
# the ``assert_scoped_reference`` / ``assert_co_occurrence`` trios (in-memory happy path,
# both failure halves, disk read, missing-target retirement message).

_TOY_PURITY = ModulePurity(
    name="toy_gate_purity",
    module="tool_gating.py",
    forbidden_import_prefix="maestro.pr_facts",
    forbidden_names=frozenset(
        {"compute_owned_writable_branches", "known_branches", "get_open_evolution_pr_states"}
    ),
    message="the gate consumes the injected allowed_branches set; it never re-derives ownership",
)

# A pure gate module: unrelated absolute import, a relative import (``node.module is
# None`` — the prefix predicate's guard branch), and a membership check over the
# injected set naming no forbidden primitive.
_PURE_MODULE = (
    "from __future__ import annotations\n"
    "from . import types\n"
    "from maestro.executor import ActionGate\n"
    "def gate(branch, allowed_branches):\n"
    "    return branch in allowed_branches\n"
)

# Impure via a direct ``maestro.pr_facts`` import (the folded-in oracle home).
_IMPORTS_PR_FACTS = (
    "from maestro.pr_facts import compute_owned_writable_branches\n"
    "def gate(branch, allowed):\n"
    "    return branch in allowed\n"
)

# Impure via a *submodule* import — caught by the prefix match, not an exact one.
_IMPORTS_PR_FACTS_SUBMODULE = (
    "from maestro.pr_facts.sub import helper\n"
    "def gate(branch, allowed):\n"
    "    return branch in allowed\n"
)

# Impure via name references only (no forbidden import): a bare ``ast.Name`` call to
# one primitive and an ``ast.Attribute`` attr to another — so the import half passes
# and the reference half fires, naming both.
_REFERENCES_PRIMITIVES = (
    "def gate(self, branch, allowed):\n"
    "    owned = compute_owned_writable_branches(branch, [], [])\n"
    "    return owned | self.pr_ledger.known_branches()\n"
)


def test_assert_module_purity_passes_on_pure_module() -> None:
    """A module that neither imports the prefix nor names a primitive passes.

    Drives ``assert_module_purity`` over an injected in-memory source so the unit stays
    off disk. The relative ``from . import types`` (``node.module is None``) and the
    unrelated ``maestro.executor`` import both exercise the prefix predicate's
    non-matching branches, and no forbidden name is referenced.
    """
    assert_module_purity(_TOY_PURITY, source=_PURE_MODULE)


def test_assert_module_purity_fails_on_forbidden_import() -> None:
    """A direct ``from maestro.pr_facts import ...`` fails, naming the forbidden prefix."""
    with pytest.raises(AssertionError, match="imports from the forbidden") as excinfo:
        assert_module_purity(_TOY_PURITY, source=_IMPORTS_PR_FACTS)
    message = str(excinfo.value)
    assert "tool_gating.py" in message
    assert "maestro.pr_facts" in message


def test_assert_module_purity_fails_on_forbidden_submodule_import() -> None:
    """A ``from maestro.pr_facts.sub import ...`` submodule import is caught by the prefix.

    The exact-vs-submodule distinction the prefix predicate adds over
    :func:`_imports_from`: a dotted child of the forbidden package still fails.
    """
    with pytest.raises(AssertionError, match="imports from the forbidden"):
        assert_module_purity(_TOY_PURITY, source=_IMPORTS_PR_FACTS_SUBMODULE)


@pytest.mark.parametrize(
    "source",
    [
        "import maestro.pr_facts\n",
        "import maestro.pr_facts as pf\n",
        "from maestro import pr_facts\n",
        "from maestro import executor, pr_facts\n",
        "from . import pr_facts\n",
    ],
)
def test_assert_module_purity_fails_on_every_import_spelling(source: str) -> None:
    """Every spelling that binds ``maestro.pr_facts`` fails (#4130).

    ``ast.Import`` forms, a ``from maestro import`` whose bound submodule sits in the
    alias (first or not), and a relative ``from . import`` resolved against the
    guarded module's ``maestro`` package all reach the prefix match.
    """
    with pytest.raises(AssertionError, match="imports from the forbidden"):
        assert_module_purity(_TOY_PURITY, source=source)


@pytest.mark.parametrize(
    "source",
    ["from maestro import pr_facts_other\n", "import maestro.executor\n", "import maestro\n"],
)
def test_assert_module_purity_spares_siblings_and_parent(source: str) -> None:
    """A same-prefixed sibling and the parent package stay unflagged."""
    assert_module_purity(_TOY_PURITY, source=source)


def test_assert_module_purity_resolves_relative_import_in_subpackage() -> None:
    """A module under a subdirectory resolves ``from .. import`` against its package."""
    nested = ModulePurity(
        name="nested",
        module="loop/x.py",
        forbidden_import_prefix="maestro.pr_facts",
        forbidden_names=frozenset(),
        message="nested",
    )
    assert_module_purity(nested, source="from . import pr_facts\n")
    with pytest.raises(AssertionError, match="imports from the forbidden"):
        assert_module_purity(nested, source="from .. import pr_facts\n")


def test_imported_modules_expands_every_spelling() -> None:
    """The resolver returns fully-qualified modules for each import form."""
    tree = ast.parse(
        "import maestro.pr_facts.sub\n"
        "from maestro import executor, types as t\n"
        "from ..x import y\n"
        "from . import z\n"
        "from os.path import *\n"
    )
    assert imported_modules(tree, "maestro.loop") == {
        "maestro.pr_facts.sub",
        "maestro",
        "maestro.executor",
        "maestro.types",
        "maestro.x",
        "maestro.x.y",
        "maestro.loop",
        "maestro.loop.z",
        "os.path",
    }


def test_assert_module_purity_fails_on_forbidden_name_reference() -> None:
    """A module naming a forbidden primitive (bare ``Name`` and ``Attribute``) fails.

    No forbidden import is present, so the import half passes and the reference half
    fires: both the bare-``Name`` call (``compute_owned_writable_branches``) and the
    ``Attribute`` attr (``known_branches``) are collected and named in the message.
    """
    with pytest.raises(AssertionError, match="references forbidden name") as excinfo:
        assert_module_purity(_TOY_PURITY, source=_REFERENCES_PRIMITIVES)
    message = str(excinfo.value)
    assert "compute_owned_writable_branches" in message
    assert "known_branches" in message


def test_assert_module_purity_reads_module_from_disk(mem_fs: dict[str, str]) -> None:
    """With no ``source``, the module is read from disk under ``root``.

    Covers the disk-read path (the default reader) via the in-memory ``mem_fs`` tree,
    mirroring :func:`assert_scoped_reference`'s disk path.
    """
    root = Path("/fake/guarded")
    mem_fs[str(root / "tool_gating.py")] = _PURE_MODULE
    assert_module_purity(_TOY_PURITY, root=root)


def test_assert_module_purity_reports_missing_target_module(mem_fs: dict[str, str]) -> None:
    """A target module absent on disk fails with the curated retirement message.

    Regression against a raw ``FileNotFoundError`` on the disk-read path: the read
    asserts the target exists first, mirroring :func:`assert_subjects_exist`'s
    ``no longer exists`` retirement message. ``tool_gating.py`` is absent from the
    in-memory tree, so the assertion names it.
    """
    root = Path("/fake/guarded")
    with pytest.raises(AssertionError, match="no longer exists") as excinfo:
        assert_module_purity(_TOY_PURITY, root=root)
    assert "tool_gating.py" in str(excinfo.value)
