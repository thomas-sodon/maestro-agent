"""Gate scope — the pure owner mapping a set of changed source files to the test
modules that can reach them, by name or by direct import.

The gate execution lease (:mod:`maestro.gate_capacity`) already owns two of the
three facts about a local quality-gate run: **how wide** it may be (the
``pytest-xdist`` worker grant sized to the cgroup's measured headroom) and **how
long** it may take (the deadline derived from what the suite measurably cost at
that width). The third fact — **which suite** a run executes — has no owner at
all: ``run_quality_gates`` builds one argv for every caller, always the whole of
``tests/`` with ``--cov``, so every round of the post-implementation fix loop
re-runs the entire suite regardless of what the diff touched.

Narrowing that round is a real behaviour change with real hazards — the
convergence loop reads a strictly shrinking failing set as progress and an
all-clean read as green, so swapping in a smaller suite mid-loop could
manufacture either signal out of nothing but the narrowing. Before any of that
is safe to wire, the *selection* has to exist and be measurable. This module is
that selection and nothing more: a pure, zero-LLM, zero-SDK oracle plus one
confined disk reader, plus :func:`restrict_signature` — the pure projection that
lets a caller compare a full-scope baseline against a narrowed round's result on
the same terms. Behind ``[loop] gate_scope_impacted_enabled`` the fix loop's
inner rounds consume both; every other gate reader stays full-scope.

The selection unions two independent halves, each deliberately cheap and
falsifiable:

* **Naming convention** (:func:`_paired_test_stems`) — a changed ``maestro/
  red_root.py`` selects ``tests/test_red_root.py`` and every ``_``-bounded
  prefix sibling (``tests/test_memory_archive.py`` counts for
  ``maestro/memory.py``). The rule is the one
  :func:`maestro.audit_checks._resolve_test_stem_family` applies, but the family
  is derived from the *changed module itself* rather than from that function's
  hand-maintained registry — so a module added this week is never invisible to
  the selection. The non-``.py`` and already-a-test rejections agree with
  :func:`maestro.planner._paired_test_basename`.
* **Import graph** (:func:`_imported_modules`) — a test module that imports the
  changed module under any of the three spellings (``import maestro.red_root``,
  ``from maestro.red_root import x``, ``from maestro import red_root``) selects,
  whatever it is named. Matching is **direct-import only** — one hop, not a
  transitive walk — so a test reaching the changed module only through an
  intermediate (``tests/test_x.py`` imports ``maestro.agent``, which imports
  ``maestro.red_root``) is not selected by this half.

Both halves consider only ``test_``-prefixed modules: a ``conftest.py`` or a
``tests/support/`` helper collects no tests, so selecting one would hand a later
caller a path that runs nothing. The same rule fixes the denominator the
selected fraction is measured against (:func:`count_test_modules`).

An **empty tuple means "full"** — the sentinel a later caller reads as "do not
narrow". An empty ``changed_paths``, a diff of nothing but docs, and a diff of
nothing but test files all yield it, so a caller that cannot narrow safely
degrades to today's whole-suite behaviour by construction.

Both gate path atoms (``maestro/`` and ``tests/``) are *parameters*, filled by
the caller from :meth:`maestro.config.Config.work_target`, never hardcoded — the
same redirectability the four quality-gate readers already honour.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from maestro.gate_observation import GateRunObservation

logger = logging.getLogger(__name__)


def _normalize(path: str) -> str:
    """Normalize a repo-relative path for prefix comparison.

    Strips a leading ``./`` and collapses backslashes to ``/`` so a caller that
    hands us a ``./maestro/foo.py`` or a Windows-shaped path still matches the
    ``source_path`` prefix.
    """
    cleaned = path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _paired_test_stems(
    changed_paths: Iterable[str], source_path: str,
) -> tuple[str, ...]:
    """Derive the test-module stem family each changed source file owns.

    ``maestro/red_root.py`` → ``red_root``. Skips a non-``.py`` file, a file
    outside *source_path*, and a file whose stem already starts ``test_`` —
    the same three rejections :func:`maestro.planner._paired_test_basename`
    makes, so a test file is never bridged to a paired test of its own.

    The family comes from the changed module's own stem rather than from any
    static registry, so a newly added module participates immediately.
    """
    prefix = _normalize(source_path).rstrip("/") + "/"
    stems: list[str] = []
    for raw in changed_paths:
        rel = _normalize(raw)
        if not rel.startswith(prefix) or not rel.endswith(".py"):
            continue
        stem = rel.rsplit("/", 1)[-1][:-3]
        if not stem or stem.startswith("test_"):
            continue
        if stem not in stems:
            stems.append(stem)
    return tuple(stems)


def _changed_dotted_names(
    changed_paths: Iterable[str], source_path: str,
) -> tuple[str, ...]:
    """Derive the importable dotted name of each changed ``.py`` source file.

    ``maestro/loop/pr_remediation.py`` → ``maestro.loop.pr_remediation``. Shares
    the non-``.py`` / outside-*source_path* rejections with
    :func:`_paired_test_stems` but — unlike it — keeps a changed *test* file's
    name out of scope only by virtue of the ``source_path`` confinement: a test
    module living under ``tests/`` never starts with the source prefix.

    A package ``__init__.py`` maps to its package (``maestro/__init__.py`` →
    ``maestro``), so a change to the package root matches every test importing
    anything under it.
    """
    root = _normalize(source_path).rstrip("/")
    prefix = root + "/"
    names: list[str] = []
    for raw in changed_paths:
        rel = _normalize(raw)
        if not rel.startswith(prefix) or not rel.endswith(".py"):
            continue
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        dotted = ".".join(parts)
        if dotted and dotted not in names:
            names.append(dotted)
    return tuple(names)


def _imported_modules(tree: ast.Module) -> frozenset[str]:
    """Every dotted module name *tree* imports, across all three spellings.

    ``ast.Import`` (``import maestro.red_root``, ``as`` aliases included),
    ``ast.ImportFrom`` with a dotted module (``from maestro.red_root import x``,
    yielding ``maestro.red_root``), and ``ast.ImportFrom`` on a package with the
    module as an alias (``from maestro import red_root``, yielding both
    ``maestro`` and ``maestro.red_root``).

    Matching is **direct-import only**: this is one hop, never a transitive
    walk. A test that imports ``maestro.agent`` — which itself imports the
    changed module — is *not* selected by this half (it may still be selected by
    the stem half). Closing that gap means walking the source tree's own import
    graph, which this increment deliberately does not do.

    Relative imports (``node.level > 0``) are skipped: the tests tree is flat
    and imports absolutely, so a relative spelling carries no resolvable target
    here and guessing one would only manufacture false selections.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0 or not node.module:
                continue
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return frozenset(names)


def _matches_stem_family(test_stem: str, stems: Iterable[str]) -> bool:
    """Whether *test_stem* belongs to any changed module's stem family.

    A ``tests/test_<x>.py`` capture matches a family when it equals the stem or
    begins ``<stem>_`` — the ``_``-bounded prefix rule
    :func:`maestro.audit_checks._resolve_test_stem_family` applies, so
    ``test_memory_archive.py`` counts for ``memory`` while ``test_memorial.py``
    (a raw prefix share) does not.
    """
    return any(
        test_stem == stem or test_stem.startswith(stem + "_") for stem in stems
    )


def _matches_import(imported: frozenset[str], dotted_names: Iterable[str]) -> bool:
    """Whether *imported* reaches any changed module's dotted name.

    Matches an exact name or a submodule of it (``<dotted>.``), so a change to a
    package selects every test importing something inside it.
    """
    return any(
        name == dotted or name.startswith(dotted + ".")
        for name in imported
        for dotted in dotted_names
    )


def select_impacted_tests(
    changed_paths: Iterable[str],
    test_sources: Mapping[str, str],
    *,
    source_path: str,
    tests_path: str,
) -> tuple[str, ...]:
    """Select the test modules that can reach *changed_paths*, sorted.

    *test_sources* maps a repo-relative test path (``tests/test_memory.py``) to
    that file's source text, so the selector is table-testable without touching
    disk — the injectable-source shape
    :func:`maestro.reality_inventory.read_shipped_cli_surface` (over
    :func:`maestro.tree_retrieval.shipped_cli_surface`) already uses.

    Only ``test_``-prefixed modules under *tests_path* are candidates, for
    *both* halves: a ``conftest.py`` or a ``tests/support/`` helper collects no
    tests, so splicing one into a pytest argv would select nothing — it is the
    ``test_`` module importing the helper that has to be selected, and the
    import half selects that module on its own account only when it imports the
    changed module *directly*.

    Unions the two halves: a test whose own stem belongs to a changed module's
    ``_``-bounded stem family, and a test whose ``ast`` imports reach a changed
    module's dotted name. Returns them sorted.

    **An empty tuple means "full"** — the sentinel a later caller reads as "do
    not narrow". It is returned whenever no changed path is an in-scope source
    module (an empty diff, a docs-only diff, a tests-only diff) *and* whenever
    an in-scope diff selects nothing, so a caller can never mistake "narrowed to
    zero tests" for a licence to run zero tests.

    A test file that does not parse is skipped for the import half but still
    participates in the stem half — its name is on disk regardless of whether
    its body is readable.
    """
    paths = list(changed_paths)
    stems = _paired_test_stems(paths, source_path)
    dotted_names = _changed_dotted_names(paths, source_path)
    if not stems and not dotted_names:
        return ()

    tests_prefix = _normalize(tests_path).rstrip("/") + "/"
    selected: set[str] = set()
    for raw_path, source in test_sources.items():
        rel = _normalize(raw_path)
        if not rel.startswith(tests_prefix) or not rel.endswith(".py"):
            continue
        test_stem = rel.rsplit("/", 1)[-1][:-3]
        if not test_stem.startswith("test_"):
            continue
        if _matches_stem_family(test_stem[len("test_"):], stems):
            selected.add(rel)
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            logger.warning("gate_scope: skipping unparseable test module %s", rel)
            continue
        if _matches_import(_imported_modules(tree), dotted_names):
            selected.add(rel)
    return tuple(sorted(selected))


def count_test_modules(
    test_sources: Mapping[str, str], *, tests_path: str,
) -> int:
    """How many collectable test modules *test_sources* holds.

    The denominator the selected fraction is measured against, spelled once so
    it cannot drift from :func:`select_impacted_tests`' candidate rule: a
    ``test_``-prefixed ``.py`` module under *tests_path*. A ``conftest.py``, an
    ``__init__.py`` and a ``tests/support/`` helper are excluded — counting them
    would systematically understate the fraction.
    """
    tests_prefix = _normalize(tests_path).rstrip("/") + "/"
    total = 0
    for raw_path in test_sources:
        rel = _normalize(raw_path)
        if not rel.startswith(tests_prefix) or not rel.endswith(".py"):
            continue
        if rel.rsplit("/", 1)[-1].startswith("test_"):
            total += 1
    return total


def read_test_sources(workspace: Path, tests_path: str) -> dict[str, str]:
    """Read every ``.py`` file under ``workspace / tests_path`` as source text.

    The one confined disk reader in this module. Keys are repo-relative paths
    (``tests/test_memory.py``), so the pure selector never sees an absolute
    path. Per-file tolerance is the narrow ``(UnicodeDecodeError, OSError)``
    pair :meth:`maestro.agent.MaestroAgent._file_reference_present` uses for a
    file that may be garbage — an unreadable file is skipped with a
    ``logger.warning``, never silenced broadly, so the fail-loud allowlist and
    its handler baseline are untouched. (``SyntaxError`` belongs to the parse,
    which the pure selector owns.)
    """
    root = workspace / tests_path
    sources: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            logger.warning("gate_scope: skipping unreadable test file %s: %s", path, exc)
            continue
        try:
            rel = str(path.relative_to(workspace))
        except ValueError:
            rel = str(path)
        sources[rel] = text
    return sources


def impacted_tests(
    changed_paths: Iterable[str],
    *,
    workspace: Path,
    source_path: str,
    tests_path: str,
) -> tuple[str, ...]:
    """Disk-reading wrapper over :func:`select_impacted_tests`.

    Reads the tests tree through :func:`read_test_sources` and delegates the
    whole decision to the pure selector, so the policy half stays zero-I/O and
    table-testable. Both path atoms are parameters, filled by the caller from
    :meth:`maestro.config.Config.work_target`, never hardcoded. Returns the
    sorted selection, empty meaning "full".
    """
    return select_impacted_tests(
        changed_paths,
        read_test_sources(workspace, tests_path),
        source_path=source_path,
        tests_path=tests_path,
    )


def restrict_signature(
    observation: GateRunObservation,
    impacted: Sequence[str],
    *,
    retain_non_answer: bool = False,
) -> frozenset[str]:
    """Restrict *observation*'s failure signature to what a narrowed run can see.

    The pure companion to :func:`impacted_tests`, and the reason narrowing a fix
    round cannot manufacture progress out of nothing: the convergence loop compares
    a round's failing set against the previous one, so a full-scope baseline must
    first be projected onto the *same* suite the narrowed round will execute.
    Identities the narrowed probe could never re-observe are dropped from the
    baseline rather than being silently cleared by the narrowing.

    The rule, per gate:

    * **Not the ``tests`` gate** — ruff and mypy still run whole under a narrowed
      round (only the pytest argv is narrowed), so their identities are retained
      verbatim, synthetic ones included — in the position-free
      :meth:`~maestro.gate_observation.GateObservation.progress_signature`
      vocabulary the convergence loop compares, so the narrowed loop compares
      like with like.
    * **The ``tests`` gate** — only the failures whose
      :attr:`~maestro.gate_observation.GateFailure.location` names a module in
      *impacted* survive. The coverage identity (``location == ""``,
      ``identifier == "coverage"``) is dropped because a narrowed round runs
      ``--no-cov``, and — when *retain_non_answer* is false — so is every
      synthetic ``"<tool>:<kind>"`` identity a non-answer contributes, because a
      killed whole-suite run says nothing about whether the narrowed probe
      answers.

    *retain_non_answer* selects which of the two directions the projection is
    being used for, and the two are not symmetric:

    * **Projecting a full-scope baseline** onto the narrowed suite (the default,
      ``False``) — the whole-suite run's "and it did not answer" fact is about a
      suite the narrowed probe will not run, so it is dropped.
    * **Restricting a narrowed round's own result** (``True``) — the run that did
      not answer *is* the narrowed probe, so the synthetic identity is retained
      exactly where :meth:`~maestro.gate_observation.GateObservation.signature`
      would add it (a non-answer kind, or a non-``CLEAN`` gate that enumerated
      nothing). Dropping it here would let a narrowed probe that timed out with
      no in-scope survivors yield the empty set — read by the convergence loop as
      maximal progress, and re-baselined to empty — from a gate that never
      finished.

    An **empty result is the "do not narrow" signal** the caller reads: the red
    lies wholly outside the impacted set (a lone ``tests:TIMEOUT`` included), so
    there is no baseline to compare a narrowed round against and the loop runs
    full-scope exactly as it does today. Pure and zero-I/O.
    """
    impacted_paths = {_normalize(path) for path in impacted}
    impacted_bases = {path.rsplit("/", 1)[-1] for path in impacted_paths}
    identities: set[str] = set()
    for gate in observation.gates:
        if gate.tool != "tests":
            identities |= gate.progress_signature()
            continue
        for failure in gate.failures:
            if failure.identifier == "coverage":
                continue
            location = _normalize(failure.location)
            if not location:
                continue
            in_scope = location in impacted_paths or (
                "/" not in location and location in impacted_bases
            )
            if in_scope:
                identities.add(failure.identity())
        # Asked of the gate itself rather than re-deriving the rule: the synthetic
        # identity is retained exactly when the owner's own ``signature()`` emits
        # it, so the two cannot drift.
        synthetic = f"{gate.tool}:{gate.kind}"
        if retain_non_answer and synthetic in gate.signature():
            identities.add(synthetic)
    return frozenset(identities)
