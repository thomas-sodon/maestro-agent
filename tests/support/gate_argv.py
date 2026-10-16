"""Locate the gate tool a ``list``/``tuple`` argv literal invokes.

Shared by the two gate-spelling guards (``tests/test_gate_command_owner.py``
and ``tests/test_gate_lease_owner.py``) so both recognise the same argv shapes.
The readers spell a gate argv two ways: tool-first (``["pytest", ...]``, with
``[sys.executable, "-m", *argv]`` prepended at run time) and interpreter-first
(``[sys.executable, "-m", "pytest", ...]`` written directly in the literal).
A guard that only read element 0 would be blind to the second shape.
"""

from __future__ import annotations

import ast


def string_constant(node: ast.expr) -> str | None:
    """Return the value of *node* iff it is a string constant, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def gate_tool(node: ast.AST) -> str | None:
    """Return the tool name an argv literal invokes, else ``None``.

    Recognises ``["<tool>", ...]`` and ``[<non-constant>, "-m", "<tool>", ...]``
    (the interpreter slot is any non-constant expression, e.g.
    ``sys.executable``). Any other node — not a list/tuple, empty, or a
    different shape — yields ``None``.
    """
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return None
    elts = node.elts
    first = string_constant(elts[0])
    if first is not None:
        return first
    if (
        not isinstance(elts[0], ast.Constant)
        and len(elts) >= 3
        and string_constant(elts[1]) == "-m"
    ):
        return string_constant(elts[2])
    return None
