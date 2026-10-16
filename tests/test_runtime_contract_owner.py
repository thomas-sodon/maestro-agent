"""AST regression: every executable ``maestro/`` spells is declared in the contract.

Proposal 141, step 3. ``deploy/runtime_contract.toml`` is the single checked-in
statement of what the production container must provide, but nothing bound the
code to it: a new ``subprocess.run(["jq", ...])`` could merge without a ``jq``
entry and the image would find out at runtime. This guard is the
declare-before-use half for executables. It walks ``maestro/`` and collects
every statically spelled binary from five shapes:

* a ``list``/``tuple`` argv literal passed to a ``subprocess`` call;
* the same literal routed through an argv wrapper — any function that forwards
  one of its own parameters as a ``subprocess`` argv (``GitVCS._run``,
  ``_run_gate_command``, ...), passed positionally or by keyword;
* the first positional literal of ``asyncio.create_subprocess_exec``;
* the literal argument of ``shutil.which``;
* a module-level ``*_COMMAND`` argv constant (``_BASE_COMMAND = ("claude",)``).

and fails naming each binary that is not a ``binary``/``toolbelt`` entry, with
the files that spell it. Argv routed through injected runner callables stays
out of reach by construction. Each shape carries an in-memory seeded-offender
control per ``tests/test_fault_seeding.py``, plus negative controls pinning
its boundary, so the guard cannot pass vacuously.

The path half binds the other two runtime facts code can silently assume:

* a kernel/host path literal (``/sys/fs/cgroup``, ``/proc``,
  ``/var/lib/docker``) passed to ``Path``/``pathlib.Path``/``open``/``os.open``
  must appear in some ``readable_fact`` entry's ``paths``;
* a module using the runtime user's home directory — ``Path.home()``,
  ``os.path.expanduser("~...")`` or ``Path("~...").expanduser()`` — must appear
  in some ``writable_path`` entry's ``used_by``.

Both fail the other way too: a declared path no file spells, or a ``used_by``
module that no longer touches the home, is stale and must be removed. Paths
that arrive through config (``Path(value).expanduser()``) stay out of view by
construction, mirroring the argv half's dynamic-argv boundary.
"""

from __future__ import annotations

import ast
import functools
from dataclasses import dataclass

import pytest

from maestro.runtime_contract import load_contract
from tests.support.gate_sites import MAESTRO_ROOT, REPO_ROOT

# The ``subprocess`` entry points whose first argument (or ``args=``) is argv.
_SUBPROCESS_CALLS: frozenset[str] = frozenset(
    {"run", "Popen", "check_output", "check_call", "call"},
)

# The contract kinds that declare an executable resolved on PATH.
_EXECUTABLE_KINDS: frozenset[str] = frozenset({"binary", "toolbelt"})

# Path prefixes the kernel/host half of the guard watches.
_HOST_PATH_PREFIXES: tuple[str, ...] = ("/sys/fs/cgroup", "/proc", "/var/lib/docker")

# Kernel/host paths and home users today's tree must keep spelling.
_FLOOR_PATHS: frozenset[str] = frozenset(
    {"/proc/meminfo", "/var/lib/docker", "/sys/fs/cgroup/cpu.max"},
)
_FLOOR_HOME_USERS: frozenset[str] = frozenset(
    {"maestro/config.py", "maestro/providers/cli_paced.py"},
)

# Binaries today's tree must keep spelling — the non-vacuity floor.
_FLOOR_BINARIES: frozenset[str] = frozenset(
    {"git", "gh", "claude", "pytest", "ruff", "mypy"},
)


@dataclass(frozen=True)
class _Wrapper:
    """Where an argv wrapper takes its argv: call-site position and name."""

    position: int
    param: str


def _is_attr_call(node: ast.Call, module: str, names: frozenset[str]) -> bool:
    """Whether *node* calls ``<module>.<one of names>``."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in names
        and isinstance(func.value, ast.Name)
        and func.value.id == module
    )


def _argv_arg(node: ast.Call, position: int, param: str) -> ast.expr | None:
    """The argument *node* passes at *position* or as keyword *param*."""
    if len(node.args) > position:
        return node.args[position]
    for keyword in node.keywords:
        if keyword.arg == param:
            return keyword.value
    return None


def _called_name(node: ast.Call) -> str | None:
    """The bare or attribute name *node* calls (``_run`` for ``self._run``)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _leading_literal(node: ast.expr | None) -> str | None:
    """The leading string literal of a ``list``/``tuple`` literal, if any."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return None
    head = node.elts[0]
    if isinstance(head, ast.Constant) and isinstance(head.value, str):
        return head.value
    return None


def _parse(sources: dict[str, str]) -> dict[str, ast.Module]:
    """Parse each ``{path: source}`` once, so both detectors share the trees."""
    return {rel: ast.parse(source, filename=rel) for rel, source in sources.items()}


class _WrapperCollector(ast.NodeVisitor):
    """One pass that binds each subprocess argv parameter to its function."""

    def __init__(self) -> None:
        self.stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.wrappers: dict[str, set[_Wrapper]] = {}

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        argv = (
            _argv_arg(node, 0, "args")
            if _is_attr_call(node, "subprocess", _SUBPROCESS_CALLS)
            else None
        )
        if isinstance(argv, ast.Name):
            for func in self.stack:
                params = [a.arg for a in (*func.args.posonlyargs, *func.args.args)]
                if argv.id in params:
                    offset = 1 if params[0] in {"self", "cls"} else 0
                    self.wrappers.setdefault(func.name, set()).add(_Wrapper(
                        position=params.index(argv.id) - offset, param=argv.id,
                    ))
        self.generic_visit(node)


def _collect_argv_wrappers(
    trees: dict[str, ast.Module],
) -> dict[str, set[_Wrapper]]:
    """Every function that forwards one of its parameters as subprocess argv."""
    collector = _WrapperCollector()
    for tree in trees.values():
        collector.visit(tree)
    return collector.wrappers


def _module_command_literals(tree: ast.Module) -> list[str]:
    """Leading literals of module-level ``*_COMMAND`` argv constants."""
    found: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        named = any(
            isinstance(t, ast.Name) and t.id.endswith("_COMMAND") for t in targets
        )
        head = _leading_literal(value)
        if named and head is not None:
            found.append(head)
    return found


def _local_argv_bindings(scope: ast.AST) -> dict[str, list[str]]:
    """Argv names bound inside *scope*'s own body, to their leading literals.

    Covers ``name = [<literal>, ...]`` and ``for name in [[<literal>, ...], ...]:``
    — the loop form binds the name to every iterated argv's leading literal.
    """
    bound: dict[str, list[str]] = {}
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        stmt = pending.pop()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        pending.extend(ast.iter_child_nodes(stmt))
        if isinstance(stmt, ast.Assign):
            targets, heads = stmt.targets, [_leading_literal(stmt.value)]
        elif isinstance(stmt, (ast.For, ast.AsyncFor)) and isinstance(
            stmt.iter, (ast.List, ast.Tuple),
        ):
            targets, heads = [stmt.target], [_leading_literal(e) for e in stmt.iter.elts]
        else:
            continue
        literals = [head for head in heads if head is not None]
        if not literals:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bound[target.id] = literals
    return bound


def _argv_heads(node: ast.expr | None, bound: dict[str, list[str]]) -> list[str]:
    """The leading literals of an argv literal or of a name bound to them."""
    if isinstance(node, ast.Name):
        return bound.get(node.id, [])
    head = _leading_literal(node)
    return [] if head is None else [head]


def _call_literals(
    node: ast.Call, wrappers: dict[str, set[_Wrapper]], bound: dict[str, list[str]],
) -> list[str]:
    """The binaries *node* spells, if it is one of the call shapes."""
    if _is_attr_call(node, "subprocess", _SUBPROCESS_CALLS):
        return _argv_heads(_argv_arg(node, 0, "args"), bound)
    if _is_attr_call(node, "asyncio", frozenset({"create_subprocess_exec"})):
        head = node.args[0] if node.args else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return [head.value]
        return []
    if _is_attr_call(node, "shutil", frozenset({"which"})):
        head = node.args[0] if node.args else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return [head.value]
        return []
    return [
        head
        for w in wrappers.get(_called_name(node) or "", set())
        for head in _argv_heads(_argv_arg(node, w.position, w.param), bound)
    ]


def _spelled_binaries(
    trees: dict[str, ast.Module], wrappers: dict[str, set[_Wrapper]],
) -> dict[str, set[str]]:
    """Map each statically spelled binary to the files that spell it."""
    spelled: dict[str, set[str]] = {}
    for rel, tree in trees.items():
        heads = _module_command_literals(tree)
        # One pass: each call resolves argv names against the bindings of its
        # innermost enclosing function (the module for top-level calls).
        pending: list[tuple[ast.AST, dict[str, list[str]]]] = [
            (tree, _local_argv_bindings(tree)),
        ]
        while pending:
            scope, bound = pending.pop()
            for node in ast.iter_child_nodes(scope):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pending.append((node, _local_argv_bindings(node)))
                    continue
                if isinstance(node, ast.Call):
                    heads.extend(_call_literals(node, wrappers, bound))
                pending.append((node, bound))
        for head in heads:
            spelled.setdefault(head, set()).add(rel)
    return spelled


def _maestro_sources() -> dict[str, str]:
    """Every ``maestro/**/*.py`` source, keyed repo-relative."""
    return {
        str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(MAESTRO_ROOT.rglob("*.py"))
    }


def _str_arg0(node: ast.Call) -> str | None:
    """The first positional argument of *node*, when it is a string literal."""
    head = node.args[0] if node.args else None
    if isinstance(head, ast.Constant) and isinstance(head.value, str):
        return head.value
    return None


def _is_path_ctor(func: ast.expr) -> bool:
    """Whether *func* names ``Path`` or ``pathlib.Path``."""
    if isinstance(func, ast.Name):
        return func.id == "Path"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "Path"
        and isinstance(func.value, ast.Name)
        and func.value.id == "pathlib"
    )


def _is_home_use(node: ast.Call) -> bool:
    """Whether *node* reaches the runtime user's home directory statically."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "home":
        return _is_path_ctor(func.value)
    if func.attr == "expanduser":
        if isinstance(func.value, ast.Call) and _is_path_ctor(func.value.func):
            return (_str_arg0(func.value) or "").startswith("~")
        is_os_path = (
            isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
        )
        return is_os_path and (_str_arg0(node) or "").startswith("~")
    return False


def _host_path_literal(node: ast.Call) -> str | None:
    """The kernel/host path literal *node* opens or wraps, if any."""
    func = node.func
    opener = (
        _is_path_ctor(func)
        or (isinstance(func, ast.Name) and func.id == "open")
        or _is_attr_call(node, "os", frozenset({"open"}))
    )
    literal = _str_arg0(node) if opener else None
    if literal is not None and literal.startswith(_HOST_PATH_PREFIXES):
        return literal
    return None


def _spelled_paths(
    trees: dict[str, ast.Module],
) -> tuple[dict[str, set[str]], set[str]]:
    """Kernel/host path literals → spelling files, and the home-using files."""
    paths: dict[str, set[str]] = {}
    home_users: set[str] = set()
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            literal = _host_path_literal(node)
            if literal is not None:
                paths.setdefault(literal, set()).add(rel)
            if _is_home_use(node):
                home_users.add(rel)
    return paths, home_users


@functools.cache
def _trees() -> dict[str, ast.Module]:
    """Today's ``maestro/`` trees, parsed once and shared by both halves."""
    return _parse(_maestro_sources())


@functools.cache
def _path_scan() -> tuple[dict[str, set[str]], set[str]]:
    """The kernel/host paths and home users of today's tree, computed once."""
    return _spelled_paths(_trees())


@functools.cache
def _tree_scan() -> tuple[dict[str, set[_Wrapper]], dict[str, set[str]]]:
    """The wrappers and spelled binaries of today's tree, computed once."""
    trees = _trees()
    wrappers = _collect_argv_wrappers(trees)
    return wrappers, _spelled_binaries(trees, wrappers)


def _declared_executables() -> set[str]:
    """Names of every ``binary``/``toolbelt`` entry in the shipped contract."""
    return {
        entry.name
        for entry in load_contract().entries
        if entry.kind in _EXECUTABLE_KINDS
    }


def _detect(source: str) -> dict[str, set[str]]:
    """Run both detectors over one synthetic module."""
    trees = _parse({"maestro/synthetic.py": source})
    return _spelled_binaries(trees, _collect_argv_wrappers(trees))


def test_every_spelled_binary_is_declared() -> None:
    _, spelled = _tree_scan()
    declared = _declared_executables()
    undeclared = {
        name: files
        for name, files in spelled.items()
        if name not in declared
    }
    lines = [f"  {name}: {', '.join(sorted(files))}" for name, files in sorted(undeclared.items())]
    assert undeclared == {}, (
        "executables spelled in maestro/ but not declared in the runtime contract:\n"
        + "\n".join(lines)
        + "\nadd a `binary` or `toolbelt` entry to deploy/runtime_contract.toml"
    )


def test_detector_floor_is_not_vacuous() -> None:
    wrappers, spelled = _tree_scan()
    assert "_run" in wrappers
    assert set(spelled) >= _FLOOR_BINARIES


_WRAPPER_PREAMBLE = """
import subprocess

class Host:
    def _run(self, cmd, *, check=True):
        return subprocess.run(cmd, check=check)
"""


def test_seeded_direct_subprocess_is_caught() -> None:
    found = _detect('import subprocess\nsubprocess.run(["jq", "."])\n')
    assert found == {"jq": {"maestro/synthetic.py"}}


def test_seeded_subprocess_args_keyword_is_caught() -> None:
    found = _detect('import subprocess\nsubprocess.Popen(args=("jq",))\n')
    assert found == {"jq": {"maestro/synthetic.py"}}


def test_seeded_wrapper_positional_is_caught() -> None:
    source = _WRAPPER_PREAMBLE + """
    def go(self):
        return self._run(["jq", "."])
"""
    assert _detect(source) == {"jq": {"maestro/synthetic.py"}}


def test_seeded_wrapper_keyword_is_caught() -> None:
    source = _WRAPPER_PREAMBLE + """
    def go(self):
        return self._run(cmd=["jq"])
"""
    assert _detect(source) == {"jq": {"maestro/synthetic.py"}}


def test_wrapper_position_skips_leading_label() -> None:
    source = """
import subprocess

def _gate(label, cmd):
    return subprocess.run(cmd)

_gate("lint", ["jq"])
"""
    assert _detect(source) == {"jq": {"maestro/synthetic.py"}}


def test_seeded_create_subprocess_exec_is_caught() -> None:
    found = _detect('import asyncio\nasyncio.create_subprocess_exec("jq", ".")\n')
    assert found == {"jq": {"maestro/synthetic.py"}}


def test_seeded_shutil_which_is_caught() -> None:
    found = _detect('import shutil\nshutil.which("jq")\n')
    assert found == {"jq": {"maestro/synthetic.py"}}


def test_seeded_command_constant_is_caught() -> None:
    assert _detect('_JQ_COMMAND = ("jq", ".")\n') == {"jq": {"maestro/synthetic.py"}}


def test_seeded_annotated_command_constant_is_caught() -> None:
    found = _detect('_JQ_COMMAND: tuple[str, ...] = ("jq",)\n')
    assert found == {"jq": {"maestro/synthetic.py"}}


def test_join_list_literal_is_not_a_binary() -> None:
    assert _detect('x = ", ".join(["jq"])\n') == {}


def test_slash_command_string_is_not_a_binary() -> None:
    assert _detect('_SLASH_COMMAND = "/jq"\n') == {}


def test_unrelated_run_is_not_a_wrapper() -> None:
    source = """
class Other:
    def _run(self, cmd):
        return len(cmd)

    def go(self):
        return self._run(["jq"])
"""
    assert _detect(source) == {}


def test_non_literal_heads_are_ignored() -> None:
    source = """
import asyncio
import shutil
import subprocess
import sys

subprocess.run([sys.executable, "-m", "x"])
subprocess.run(cmd)
asyncio.create_subprocess_exec(sys.executable)
asyncio.create_subprocess_exec()
shutil.which(name)
shutil.which()
_X_COMMAND = (sys.executable,)
_Y_COMMAND = ()
_Z: int
"""
    assert _detect(source) == {}


def test_seeded_local_argv_binding_is_caught() -> None:
    source = _WRAPPER_PREAMBLE + """
    def go(self):
        cmd = ["jq", "."]
        return self._run(cmd)
"""
    assert _detect(source) == {"jq": {"maestro/synthetic.py"}}


def test_same_named_wrappers_are_all_tried() -> None:
    source = """
import subprocess

def _run(cmd):
    return subprocess.run(cmd)

class Host:
    def _run(self, label, cmd):
        return subprocess.run(cmd)

_run(["jq"])
Host()._run("x", ["yq"])
"""
    assert _detect(source) == {
        "jq": {"maestro/synthetic.py"}, "yq": {"maestro/synthetic.py"},
    }


def test_seeded_for_loop_argv_binding_is_caught() -> None:
    source = """
import subprocess

for cmd in [["jq", "."], ("yq",), [name]]:
    subprocess.run(cmd)
"""
    assert _detect(source) == {
        "jq": {"maestro/synthetic.py"}, "yq": {"maestro/synthetic.py"},
    }


def test_for_loop_over_non_argv_is_ignored() -> None:
    source = """
import subprocess

for cmd in [name, [other]]:
    subprocess.run(cmd)
for cmd in names:
    subprocess.run(cmd)
"""
    assert _detect(source) == {}


# ── kernel/host paths and the home directory ────────────────────────


def _declared_paths() -> set[str]:
    return {
        path
        for entry in load_contract().entries
        if entry.kind == "readable_fact"
        for path in entry.paths
    }


def _declared_home_users() -> set[str]:
    return {
        module
        for entry in load_contract().entries
        if entry.kind == "writable_path"
        for module in entry.used_by
    }


def _detect_paths(source: str) -> tuple[dict[str, set[str]], set[str]]:
    return _spelled_paths(_parse({"maestro/synthetic.py": source}))


def test_every_spelled_host_path_is_declared() -> None:
    spelled, _ = _path_scan()
    declared = _declared_paths()
    undeclared = {p: files for p, files in spelled.items() if p not in declared}
    lines = [f"  {p}: {', '.join(sorted(f))}" for p, f in sorted(undeclared.items())]
    assert undeclared == {}, (
        "kernel/host paths spelled in maestro/ but not declared in the runtime "
        "contract:\n" + "\n".join(lines)
        + "\nadd them to a `readable_fact` entry's `paths`"
    )


def test_every_home_user_is_declared() -> None:
    _, home_users = _path_scan()
    undeclared = sorted(home_users - _declared_home_users())
    assert undeclared == [], (
        "maestro/ modules using the home directory but not declared in the "
        f"runtime contract: {undeclared}\nadd them to a `writable_path` "
        "entry's `used_by`"
    )


def test_no_stale_path_declarations() -> None:
    spelled, home_users = _path_scan()
    assert sorted(_declared_paths() - set(spelled)) == []
    assert sorted(_declared_home_users() - home_users) == []


def test_path_floor_is_not_vacuous() -> None:
    spelled, home_users = _path_scan()
    assert set(spelled) >= _FLOOR_PATHS
    assert home_users >= _FLOOR_HOME_USERS


@pytest.mark.parametrize(
    ("source", "literal"),
    [
        ('from pathlib import Path\nPath("/proc/pressure/cpu")\n', "/proc/pressure/cpu"),
        ('import pathlib\npathlib.Path("/var/lib/docker/x")\n', "/var/lib/docker/x"),
        ('open("/sys/fs/cgroup/pids.max")\n', "/sys/fs/cgroup/pids.max"),
        ('import os\nos.open("/proc/stat", 0)\n', "/proc/stat"),
    ],
)
def test_seeded_host_path_is_caught(source: str, literal: str) -> None:
    assert _detect_paths(source) == ({literal: {"maestro/synthetic.py"}}, set())


@pytest.mark.parametrize(
    "source",
    [
        'from pathlib import Path\nPath.home() / ".cache"\n',
        'import pathlib\npathlib.Path.home()\n',
        'import os\nos.path.expanduser("~/.cache")\n',
        'from pathlib import Path\nPath("~/.cache").expanduser()\n',
    ],
)
def test_seeded_home_use_is_caught(source: str) -> None:
    assert _detect_paths(source) == ({}, {"maestro/synthetic.py"})


def test_path_negative_controls_are_ignored() -> None:
    source = """
import os
from pathlib import Path

Path("/tmp/x")
Path(name)
Path()
Path(value).expanduser()
Path("/etc").expanduser()
os.path.expanduser(value)
os.path.expanduser("/abs")
other.path.expanduser("~")
x.expanduser()
Thing.home()
_PROC = "/proc/meminfo"
open(name)
print("/proc/meminfo")
"""
    assert _detect_paths(source) == ({}, set())
