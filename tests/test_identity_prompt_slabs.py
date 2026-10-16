"""AST regression: volatile fields must stay out of the stable identity slab.

The cache-stratified ``get_identity_prompt()`` relies on
``_stable_identity_block()`` returning a slab whose contents change
*at most every few cycles*. If a future refactor inserts
``evolution_count``, ``open_questions``, or ``limitations_known`` into
that block, prefix caching silently breaks: the metric would degrade
without any test failing.

This file walks the AST of ``maestro.consciousness`` and fails when
``_stable_identity_block`` references any of those volatile fields. It
also confirms ``_volatile_identity_block`` *does* reference them — so
that an accidental swap of the two methods is caught immediately.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CONSCIOUSNESS_PY = (
    Path(__file__).resolve().parent.parent / "maestro" / "consciousness.py"
)

# Fields that churn every loop and therefore must NEVER appear in the
# stable slab. ``evolution_count`` increments on every ``evolve()``;
# ``open_questions`` churns from audit + REMOVE/REFINE/KEEP;
# ``limitations_known`` churns from the limitation→question bridge.
_VOLATILE_FIELDS: frozenset[str] = frozenset({
    "evolution_count",
    "open_questions",
    "limitations_known",
})


def _find_method(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {name!r} not found in consciousness.py")


def _attribute_names(node: ast.AST) -> set[str]:
    """Return every attribute name read inside *node*.

    A ``self.foo`` access yields ``"foo"``; a ``model.bar`` access
    yields ``"bar"``. The names of fields like ``model.evolution_count``
    are what we want to guard against, not arbitrary local-variable
    references that happen to share the name.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def test_stable_slab_excludes_volatile_fields() -> None:
    """``_stable_identity_block`` must not touch any volatile field.

    Caught early, this is a one-line revert; caught late (via the
    ``cache_hit_rate`` metric in production), this is a multi-day
    operational debug.
    """
    source = _CONSCIOUSNESS_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = _find_method(tree, "_stable_identity_block")
    referenced = _attribute_names(method)

    leaks = referenced & _VOLATILE_FIELDS
    assert not leaks, (
        "Volatile fields must not appear in the stable identity slab "
        "(they would invalidate the prefix cache on every loop "
        f"iteration). Leaked: {sorted(leaks)}. Move them to "
        "_volatile_identity_block instead."
    )


def test_volatile_slab_owns_volatile_fields() -> None:
    """Counterpart sanity check: the volatile slab references all of them.

    Without this, swapping the two block methods (so the stable one
    *becomes* the volatile one) would pass
    :func:`test_stable_slab_excludes_volatile_fields` while still
    silently wrecking cache hit rate.
    """
    source = _CONSCIOUSNESS_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = _find_method(tree, "_volatile_identity_block")
    referenced = _attribute_names(method)

    missing = _VOLATILE_FIELDS - referenced
    assert not missing, (
        "_volatile_identity_block must surface every volatile field so "
        "the stable slab can stay churn-free. Missing: "
        f"{sorted(missing)}."
    )
