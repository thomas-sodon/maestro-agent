"""Patch strings name a module that actually owns the patched name (proposal 147 Step 12).

The retired ``maestro.loop._agent_module()`` seam resolved loop-collaborator
symbols on :mod:`maestro.agent` at call time, so a ``patch("maestro.agent.X")``
kept intercepting even after ``X``'s consumer moved out of ``agent.py``. With
the seam gone, such a string only intercepts when the patched module itself
reads the name. This guard fails a ``patch`` / ``setattr`` string literal
aimed at ``maestro.agent``, ``maestro.evolution`` or ``maestro.audit_checks``
whose first attribute is neither defined at module level there nor an
imported binding the module still reads — a keep-alive re-export that only
existed for the seam.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TESTS_ROOT = _REPO_ROOT / "tests"
_CHECKED_MODULES = ("agent", "evolution", "audit_checks")
_TARGET_RE = re.compile(r"^maestro\.(agent|evolution|audit_checks)\.([A-Za-z_]\w*)")


def _module_bindings(source: str) -> tuple[set[str], set[str], set[str]]:
    """Return ``(defined, imported, referenced)`` names for a module *source*.

    ``defined`` holds module-level ``def`` / ``class`` names and assignment
    targets; ``imported`` holds module-level import bindings; ``referenced``
    holds every ``Load`` name anywhere in the module (an import statement
    binds, it never loads, so a re-export alone is not a reference).
    """
    tree = ast.parse(source)
    defined: set[str] = set()
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        defined.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[0])
    referenced = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return defined, imported, referenced


def _patch_targets(source: str) -> list[tuple[str, str, int]]:
    """Return ``(module, head, lineno)`` for each checked patch-string literal.

    Any call whose first positional argument is a string constant matching
    ``maestro.<checked module>.<name>...`` counts — ``patch(...)``,
    ``mock.patch(...)``, ``mocker.patch(...)`` and ``monkeypatch.setattr("...")``
    alike. ``head`` is the first attribute segment (``subprocess`` in
    ``maestro.agent.subprocess.run``). Non-literal arguments are skipped.
    """
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        match = _TARGET_RE.match(first.value)
        if match is not None:
            found.append((match.group(1), match.group(2), node.lineno))
    return found


def _resolves(head: str, bindings: tuple[set[str], set[str], set[str]]) -> bool:
    """True when *head* is defined in the module, or imported and still read there."""
    defined, imported, referenced = bindings
    return head in defined or (head in imported and head in referenced)


def _unresolved(
    targets: list[tuple[str, str, int]],
    bindings: dict[str, tuple[set[str], set[str], set[str]]],
) -> list[tuple[str, str, int]]:
    """Return the targets whose head does not resolve in their module."""
    return [t for t in targets if not _resolves(t[1], bindings[t[0]])]


def test_seeded_violations_are_flagged() -> None:
    """Only the imported-but-unread name is flagged; dotted and non-literal cases behave."""
    module_source = (
        "import subprocess\n"
        "from maestro.sdk import query\n"
        "from maestro.contributions import check_merge_clean\n"
        "LIMIT = 3\n"
        "def f():\n"
        "    subprocess.run(['x'])\n"
        "    return query()\n"
    )
    bindings = {"agent": _module_bindings(module_source)}
    test_source = (
        "patch('maestro.agent.f')\n"
        "patch('maestro.agent.query')\n"
        "mock.patch('maestro.agent.check_merge_clean')\n"
        "monkeypatch.setattr('maestro.agent.subprocess.run', fake)\n"
        "patch('maestro.agent.LIMIT', 4)\n"
        "patch(f'maestro.agent.{name}')\n"
        "patch(target)\n"
        "patch()\n"
        "patch('maestro.memory.check_merge_clean')\n"
    )
    targets = _patch_targets(test_source)
    assert len(targets) == 5
    assert _unresolved(targets, bindings) == [("agent", "check_merge_clean", 3)]


@pytest.mark.timeout(15)
def test_every_patch_target_resolves_to_its_home() -> None:
    """Every live patch string on a checked module names a name that module owns or reads."""
    bindings = {
        name: _module_bindings(
            (_REPO_ROOT / "maestro" / f"{name}.py").read_text(encoding="utf-8"),
        )
        for name in _CHECKED_MODULES
    }
    offenders = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        targets = _patch_targets(path.read_text(encoding="utf-8"))
        for module, head, lineno in _unresolved(targets, bindings):
            rel = path.relative_to(_REPO_ROOT)
            offenders.append(f"{rel}:{lineno} maestro.{module}.{head}")
    assert offenders == [], (
        "patch strings aimed at a module that neither defines nor reads the name "
        "no longer intercept anything; repoint each to the consumer module that "
        "reads it: " + ", ".join(offenders)
    )


def test_agent_module_shim_is_gone() -> None:
    """The ``_agent_module`` call-time seam stays deleted."""
    assert not hasattr(importlib.import_module("maestro.loop"), "_agent_module")
