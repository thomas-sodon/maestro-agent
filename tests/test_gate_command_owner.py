"""AST regression: no reader may re-hardcode a quality-gate command literal.

Proposal 107, gate-collapse increment. "Which package do my gates cover?" is a
single fact owned by :class:`maestro.work_target.WorkTarget`'s two path atoms
(``gate_source_path`` / ``gate_tests_path``, defaulting to ``maestro/`` /
``tests/``). Four readers run the quality gates — ``gate_check.py``,
``red_root.py``, ``evolution.py``, and ``loop/pr_remediation.py`` — and each now
substitutes those atoms into its own reader-specific flag set rather than baking
the paths into a string-literal command list. This guard fails the build when a
reader re-introduces a hardcoded gate command: a ``list``/``tuple`` literal that
invokes ``"ruff"`` / ``"mypy"`` / ``"pytest"`` — either tool-first
(``["ruff", ...]``) or interpreter-first (``[sys.executable, "-m", "ruff", ...]``),
located by ``tests.support.gate_argv.gate_tool`` — and contains a ``"maestro/"``
or ``"tests/"`` string-constant element.

Only the four reader modules are scanned, deliberately: the atom *defaults*
legitimately live as string literals in ``maestro/config.py``, and unrelated
``"maestro/evolve/"`` branch prefixes elsewhere would otherwise register as
false positives. Because the located tool must be one of the three names,
an incidental tuple like ``("ruff", review.ruff_ok)`` (no path constant) never
trips it, and the collapsed builders — which place the atoms in as *variables*,
not string constants — pass cleanly.

Modeled on the AST-walk shape of ``tests/test_merge_truth_chokepoint.py``.
"""

from __future__ import annotations

import ast

import pytest

from tests.support.gate_argv import gate_tool, string_constant
from tests.support.gate_sites import MAESTRO_ROOT, PYTHON_GATE_READERS

# Repo-relative root of the package under guard, and the four gate readers that
# must source their gate paths from the WorkTarget owner rather than a hardcoded
# command literal. Both come from the shared gate-site owner (proposal 126, step
# 2) so this guard and its sibling worker-count guard scan one list, not two.
_MAESTRO_ROOT = MAESTRO_ROOT
_SCANNED: tuple[str, ...] = PYTHON_GATE_READERS

# The gate-tool names a hardcoded command list begins with.
_GATE_TOOLS: frozenset[str] = frozenset({"ruff", "mypy", "pytest"})

# The path atoms a hardcoded command would embed instead of substituting the
# WorkTarget's redirectable atoms.
_HARDCODED_PATHS: frozenset[str] = frozenset({"maestro/", "tests/", "scripts/"})


def _is_hardcoded_gate_literal(node: ast.AST) -> bool:
    """True iff *node* is a gate-command literal with a hardcoded path atom.

    The signature of a hardcoded gate command: a ``list``/``tuple`` literal
    that invokes ``"ruff"`` / ``"mypy"`` / ``"pytest"`` (tool-first or
    ``[sys.executable, "-m", "<tool>", ...]``) and which contains at least one ``"maestro/"`` / ``"tests/"``
    string-constant element. The collapsed builders place the paths in as
    *variables* (``source`` / ``tests``), so their command tuples carry no such
    path constant and are not flagged.
    """
    if gate_tool(node) not in _GATE_TOOLS:
        return False
    assert isinstance(node, (ast.List, ast.Tuple))
    return any(
        string_constant(elt) in _HARDCODED_PATHS for elt in node.elts
    )


def _violators(source: str, rel: str) -> list[str]:
    """Return ``rel:lineno`` for every hardcoded gate literal in *source*."""
    tree = ast.parse(source, filename=rel)
    return [
        f"{rel}:{node.lineno}"
        for node in ast.walk(tree)
        if _is_hardcoded_gate_literal(node)
    ]


@pytest.mark.timeout(15)
def test_no_reader_hardcodes_a_gate_command_literal() -> None:
    """No gate reader may bake ``maestro/`` / ``tests/`` into a gate command list.

    Each reader must build its gate commands from the ``WorkTarget`` owner's
    two path atoms (via ``build_gate_commands`` / ``build_red_root_gates`` /
    the inline ``wt.gate_source_path`` reads). A re-introduced hardcoded literal
    silently re-forks the "which package do my gates cover" fact — exactly the
    regression this guard prevents.
    """
    violators: list[str] = []
    for rel in _SCANNED:
        path = _MAESTRO_ROOT / rel
        violators.extend(_violators(path.read_text(encoding="utf-8"), rel))

    assert not violators, (
        "Hardcoded quality-gate command literal re-introduced (proposal 107):\n  "
        + "\n  ".join(violators)
        + "\n\nBuild the gate commands from ``Config.work_target()``'s "
        "``gate_source_path`` / ``gate_tests_path`` atoms instead of baking "
        "``maestro/`` / ``tests/`` into the command list."
    )


def test_guard_flags_a_synthetic_hardcoded_literal() -> None:
    """A hand-written hardcoded gate literal is detected (non-vacuity self-test).

    Drift in the AST helper would silently make the guard above pass on real
    violators. Parse a synthetic module that re-hardcodes the ruff command and
    assert the helper flags exactly it — and that the collapsed, variable-based
    form is *not* flagged.
    """
    source = (
        "GATES = [\n"
        '    ["ruff", "check", "maestro/", "tests/"],\n'
        '    ["mypy", "maestro/"],\n'
        '    [sys.executable, "-m", "pytest", "tests/"],\n'
        '    [sys.executable, "-m", "ruff", "check", "maestro/"],\n'
        "]\n"
        "source = wt.gate_source_path\n"
        "tests = wt.gate_tests_path\n"
        "CLEAN = [\n"
        '    ["ruff", "check", source, tests],\n'
        '    ("ruff", review.ruff_ok),\n'
        '    [sys.executable, "-m", "pytest", tests, "--collect-only"],\n'
        '    [sys.executable, "-m", "git", "maestro/"],\n'
        "]\n"
    )
    tree = ast.parse(source)
    flagged = [
        node.lineno
        for node in ast.walk(tree)
        if _is_hardcoded_gate_literal(node)
    ]
    # The four hardcoded rows on lines 2-5 (tool-first and interpreter-first)
    # are flagged; the variable-based rows, the ``("ruff", review.ruff_ok)``
    # incidental tuple, and the non-gate ``-m git`` row are not.
    assert flagged == [2, 3, 4, 5]


def test_guard_reaches_the_interpreter_first_collect_probe() -> None:
    """Seeding a literal path into ``gate_check.py``'s collect probe is flagged.

    The runnability probe spells its argv ``[sys.executable, "-m", "pytest",
    work_target.gate_tests_path, ...]``; swapping the atom for ``"tests/"`` in
    memory must trip the guard (issue #4050).
    """
    source = (_MAESTRO_ROOT / "gate_check.py").read_text(encoding="utf-8")
    atom = "work_target.gate_tests_path,\n"
    assert atom in source
    mutated = source.replace(atom, '"tests/",\n', 1)
    assert _violators(source, "gate_check.py") == []
    assert len(_violators(mutated, "gate_check.py")) == 1


def test_scanned_reader_modules_exist() -> None:
    """Every scanned reader path resolves to a real module.

    Guards against a rename silently emptying the scan set and hiding a
    re-hardcoded literal (mirrors ``test_merge_truth_chokepoint.py``'s
    allowlist-existence check).
    """
    for rel in _SCANNED:
        assert (_MAESTRO_ROOT / rel).is_file(), f"missing reader module: {rel}"
