"""Every ``output_format=`` literal in ``maestro/`` is ``json_schema``-enveloped.

The SDK transport emits ``--json-schema`` only for
``{"type": "json_schema", "schema": {...}}``; a bare schema is silently
dropped and the call runs unconstrained (proposal 156 W1, the scouting
brief refused ``unparseable-output`` twice in production).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "maestro"


def _module_path(module: str) -> Path | None:
    base = ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _resolve(name: str, tree: ast.Module, seen: frozenset[str] = frozenset()) -> ast.expr | None:
    """Resolve *name* to its module-level value, following ``from maestro.x import``."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name and node.value:
                return node.value
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("maestro"):
            for alias in node.names:
                if (alias.asname or alias.name) != name or node.module in seen:
                    continue
                path = _module_path(node.module)
                if path is None:
                    return None
                other = ast.parse(path.read_text(encoding="utf-8"))
                return _resolve(alias.name, other, seen | {node.module})
    return None


def _is_enveloped(node: ast.expr) -> bool:
    if not isinstance(node, ast.Dict):
        return False
    entries = {
        k.value: v for k, v in zip(node.keys, node.values, strict=True)
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }
    kind = entries.get("type")
    return isinstance(kind, ast.Constant) and kind.value == "json_schema" and "schema" in entries


_Scope = ast.FunctionDef | ast.AsyncFunctionDef


def _params(fn: _Scope) -> set[str]:
    a = fn.args
    names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return set(names)


def _local_value(name: str, fn: _Scope) -> ast.expr | None:
    """The last function-local ``name = ...`` / ``name: T = ...`` value in *fn*."""
    found: ast.expr | None = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        )) or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            found = node.value
    return found


def _imported_module(base: str, tree: ast.Module) -> str | None:
    """The ``maestro.*`` module *base* is bound to by an import, if any."""
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("maestro") and alias.asname == base:
                    return alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("maestro"):
            for alias in node.names:
                if (alias.asname or alias.name) == base:
                    full = f"{node.module}.{alias.name}"
                    if _module_path(full) is not None:
                        return full
    return None


def _check(value: ast.expr, tree: ast.Module, fn: _Scope | None) -> bool:
    """True when *value* is (or resolves to) an enveloped schema or a pass-through."""
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    if isinstance(value, ast.Attribute):
        if isinstance(value.value, ast.Name):
            module = _imported_module(value.value.id, tree)
            if module is not None:
                path = _module_path(module)
                assert path is not None
                other = ast.parse(path.read_text(encoding="utf-8"))
                resolved = _resolve(value.attr, other, frozenset({module}))
                return resolved is not None and _is_enveloped(resolved)
        return True  # pass-through read, e.g. options.output_format
    if isinstance(value, ast.Name):
        if fn is not None:
            if value.id in _params(fn):
                return True  # parameter pass-through
            local = _local_value(value.id, fn)
            if local is not None:
                return _is_enveloped(local)
        resolved = _resolve(value.id, tree)
        return resolved is not None and _is_enveloped(resolved)
    return _is_enveloped(value)


def offending_output_formats(source: str) -> list[int]:
    """Line numbers of ``output_format=`` keywords that are not enveloped."""
    tree = ast.parse(source)
    bad: list[int] = []

    def visit(node: ast.AST, fn: _Scope | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            fn = node
        if (
            isinstance(node, ast.keyword)
            and node.arg == "output_format"
            and not _check(node.value, tree, fn)
        ):
                bad.append(node.value.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, None)
    return sorted(bad)


def test_output_format_literals_are_enveloped() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "output_format=" not in source:
            continue
        offenders += [f"{path.relative_to(ROOT)}:{n}" for n in offending_output_formats(source)]
    assert offenders == []


def test_guard_flags_bare_schema_literal_and_name() -> None:
    source = (
        "BARE = {'type': 'object'}\n"
        "GOOD = {'type': 'json_schema', 'schema': {'type': 'object'}}\n"
        "f(output_format={'type': 'object'})\n"
        "f(output_format=BARE)\n"
        "f(output_format=GOOD)\n"
        "f(output_format=UNKNOWN)\n"
        "f(output_format=opts.output_format)\n"
        "f(output_format=None)\n"
    )
    assert offending_output_formats(source) == [3, 4, 6]


def test_guard_inspects_function_locals_and_params() -> None:
    source = (
        "def bare():\n"
        "    output_format = {'type': 'object'}\n"
        "    f(output_format=output_format)\n"
        "def good():\n"
        "    output_format: dict = {'type': 'json_schema', 'schema': {}}\n"
        "    f(output_format=output_format)\n"
        "async def param(output_format, *a, **k):\n"
        "    f(output_format=output_format)\n"
        "def unbound():\n"
        "    f(output_format=output_format)\n"
    )
    assert offending_output_formats(source) == [3, 10]


def test_guard_resolves_maestro_module_attributes() -> None:
    good = "from maestro import planner\nf(output_format=planner._STEPS_OUTPUT_FORMAT)\n"
    assert offending_output_formats(good) == []
    missing = "import maestro.planner as opp\nf(output_format=opp.NO_SUCH)\n"
    assert offending_output_formats(missing) == [2]
    other = "import json as js\nimport maestro.planner\nf(output_format=js.X)\n"
    assert offending_output_formats(other) == []


def test_guard_follows_maestro_imports() -> None:
    enveloped = "from maestro.planner import _STEPS_OUTPUT_FORMAT\nf(output_format=_STEPS_OUTPUT_FORMAT)\n"
    assert offending_output_formats(enveloped) == []
    missing = "from maestro.planner import NO_SUCH_NAME\nf(output_format=NO_SUCH_NAME)\n"
    assert offending_output_formats(missing) == [2]
    no_module = "from maestro.no_such_module import X\nf(output_format=X)\n"
    assert offending_output_formats(no_module) == [2]
    annotated = "X: dict = {'type': 'json_schema', 'schema': {}}\nf(output_format=X)\n"
    assert offending_output_formats(annotated) == []
