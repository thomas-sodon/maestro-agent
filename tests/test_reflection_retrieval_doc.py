"""Guard: the reflection-retrieval docs track the shipped surface (issue #4168).

The ``docs/modules.md`` row for ``maestro/reflection_retrieval.py`` once kept
describing proposal 132's first increment ("no production caller yet", the flag
"inert: no reader") long after the surface was wired, and omitted two shipped
public symbols. Separately, prose restated the allowlist size as a spelled-out
count that went stale when ``READ_ONLY_REFLECTION_TOOLS`` grew. These tests pin
both drift classes narrowly: the row must name every public top-level symbol, and
the two reflection-surface prose sites must not restate a tool count.
"""

from __future__ import annotations

import ast
import re

from tests.test_module_doc_coverage import _MAESTRO_ROOT, _MODULES_DOC, _REPO_ROOT

_ROW = re.compile(r"^\|\s*`maestro/reflection_retrieval\.py`.*$", re.MULTILINE)
_COUNTED_TOOLS = re.compile(
    r"\b(?:eleven|twelve|thirteen|fourteen|\d+)\b[^.]{0,40}mcp__<server>__<tool>",
    re.IGNORECASE,
)


def _public_names(source: str) -> set[str]:
    """Collect the module's public top-level class, function, and constant names."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return {name for name in names if not name.startswith("_")}


def _method_docstring(source: str, class_name: str, method: str) -> str:
    """Return ``class_name.method``'s docstring, or ``""`` when absent."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method:
                    return ast.get_docstring(item) or ""
    return ""


def test_public_names_skips_private_and_collects_all_shapes() -> None:
    """The collector sees classes, defs, annotated and plain assigns, and drops ``_`` names."""
    source = (
        "A: int = 1\nB = C = 2\nx.y = 3\ndef f(): pass\nasync def g(): pass\n"
        "def _h(): pass\n_Z = 4\nclass K: pass\nclass _P: pass\n"
    )
    assert _public_names(source) == {"A", "B", "C", "K", "f", "g"}


def test_method_docstring_missing_returns_empty() -> None:
    """An absent class or method yields an empty docstring, not a crash."""
    source = "class K:\n    x = 1\n    def m(self):\n        pass\n"
    assert _method_docstring(source, "K", "m") == ""
    assert _method_docstring(source, "K", "other") == ""
    assert _method_docstring(source, "Other", "m") == ""


def test_reflection_retrieval_row_names_every_public_symbol() -> None:
    """Every public symbol of the module must be named in its modules.md row."""
    match = _ROW.search(_MODULES_DOC.read_text(encoding="utf-8"))
    assert match is not None
    row = match.group(0)
    source = (_MAESTRO_ROOT / "reflection_retrieval.py").read_text(encoding="utf-8")
    missing = sorted(name for name in _public_names(source) if name not in row)
    assert missing == []


def test_reflection_surface_docs_restate_no_tool_count() -> None:
    """Neither reflection-surface prose site may restate the allowlist size."""
    agent_source = (_MAESTRO_ROOT / "agent.py").read_text(encoding="utf-8")
    docstring = _method_docstring(agent_source, "MaestroAgent", "_build_contemplation_session_options")
    assert "mcp__<server>__<tool>" in docstring
    assert _COUNTED_TOOLS.search(docstring) is None

    sdk_doc = (_REPO_ROOT / "docs" / "sdk-internals.md").read_text(encoding="utf-8")
    paragraph = next(line for line in sdk_doc.splitlines() if line.startswith("**Grounded reflection"))
    assert _COUNTED_TOOLS.search(paragraph) is None


def test_counted_tools_regex_flags_spelled_and_numeric_counts() -> None:
    """The drift regex catches both spelled-out and numeric counts."""
    assert _COUNTED_TOOLS.search("the eleven `mcp__<server>__<tool>` names") is not None
    assert _COUNTED_TOOLS.search("the 13 ``mcp__<server>__<tool>`` names") is not None
    assert _COUNTED_TOOLS.search("the members as `mcp__<server>__<tool>` names") is None
