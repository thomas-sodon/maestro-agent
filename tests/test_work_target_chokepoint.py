"""AST regression: repo clients must build through the WorkTarget owner.

Proposal 107, client-construction chokepoint increment. "Which repo does
Maestro's effort point at?" is a single fact — a checkout to run git against
(``repo_workspace``) and an ``owner/repo`` PR target (``github_repo``) — owned by
:class:`maestro.work_target.WorkTarget` and resolved by ``Config.work_target()``.
The five loop-facing git/GitHub client-construction seams (``cli._build_agent``,
``cli._build_mcp_serve_registry``, ``cli.cmd_issues``,
``MaestroAgent.__init__``'s fallback arms, and ``EvolutionEngine.__init__``)
build their ``SubprocessVCS`` / ``SubprocessCodeHost`` / ``IssueTracker`` clients
from ``config.work_target()`` (via ``wt.repo_workspace`` / ``wt.github_repo``)
rather than the raw ``config.workspace`` / ``config.github_repo`` scalars, so a
``[work]`` redirect points every client at a customer repo while a default build
stays byte-identical. ``tests/test_work_target_clients.py`` pins that behavior.

This guard closes the *structural* gap that behavioral test cannot: it fails the
build when a scanned client constructor is fed a raw ``config.workspace`` /
``config.github_repo`` argument, so a future edit cannot silently re-scatter the
fact by adding a sixth, raw-scalar construction path at an enforced seam. It
mirrors the AST-walk shape of ``tests/test_merge_truth_chokepoint.py`` and is the
sibling of ``tests/test_gate_command_owner.py``, which defends the parallel
"which package do my gates cover?" facet with the same one-owner discipline.

The rule: for every ``ast.Call`` whose function is a bare ``Name`` in the scanned
constructor set, walk its argument subtrees (positional and keyword) and flag any
``ast.Attribute`` whose ``.attr`` is ``workspace`` / ``github_repo`` *and* whose
``.value`` is the bare ``ast.Name`` ``config``. The through-owner reads do not
match: ``wt.repo_workspace`` / ``wt.github_repo`` read a different base
(``wt``), ``config.work_target().github_repo`` reads through a ``Call`` (not a
bare ``config`` ``Name``), and ``wt.repo_workspace`` uses a different ``.attr``.

Deferred scatter — the sites left reading the raw scalar on purpose, named here
so the residual is explicit rather than silent (see the proposal's out-of-scope
list; a later PR migrates them):

* ``cli.cmd_notify_pre_deploy`` builds
  ``SubprocessCodeHost(repo=config.github_repo, workspace=config.workspace)`` in
  the pre-deploy-notice path — a deliberately non-migrated seam, carried in
  ``_FUNCTION_ALLOWLIST`` so the guard passes on the current tree while any *new*
  raw-scalar construction at an enforced seam fails.
* The two non-seam ``ContributionTracker(config.workspace, …)`` constructions
  (``MaestroAgent.__init__``, ``cli._build_agent``) read the raw scalar but use a
  constructor name *outside* this guard's scanned set, so they are not flagged.
* The direct ``cwd=config.workspace`` subprocess / path-join sites across
  ``agent.py`` (branch teardown, the gate runners, the fetch / merge-truth
  helpers, ``implement_plan``, and the issue-precondition / uncalled-symbol
  probes) and the ``cli.py`` lexicon commands read the scalar as a bare
  attribute, not as a scanned-constructor argument, so they are out of scope.
* ``agent.py`` imports ``IssueTracker`` under the alias ``_IssueTracker``; the
  scanned set carries *both* names so the ``MaestroAgent.__init__`` issues
  fallback — one of the five enforced seams — is not left unguarded by the alias.

Scope note: the guard matches only the bare-``config`` attribute form
(``config.workspace`` / ``config.github_repo``). A workspace value passed as a
*local variable* (as ``cli._resolve_pre_deploy_prs`` does with
``SubprocessVCS(workspace)``) or read off an attribute base other than the bare
``config`` ``Name`` is intentionally out of scope — today's seams read
``config.…`` / ``config.work_target()``, so the bare-``Name`` form covers the
current tree; broaden only when a new base shape appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Repo-relative root of the package under guard.
_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"

# The three modules that build the repo clients from config. Each holds one or
# more of the five enforced construction seams and so must read the WorkTarget
# owner rather than a raw ``config`` scalar.
_SCANNED: tuple[str, ...] = ("cli.py", "agent.py", "evolution.py")

# The client constructor names whose arguments this guard scans. ``_IssueTracker``
# is ``agent.py``'s import alias for ``IssueTracker`` — carried here so the
# ``MaestroAgent.__init__`` issues fallback seam is guarded despite the alias.
_CONSTRUCTOR_NAMES: frozenset[str] = frozenset({
    "SubprocessVCS",
    "SubprocessCodeHost",
    "IssueTracker",
    "_IssueTracker",
})

# The raw ``Config`` scalars a client constructor must *not* be fed directly:
# ``config.workspace`` / ``config.github_repo``. The through-owner reads
# (``wt.repo_workspace`` / ``config.work_target().github_repo``) are clean.
_RAW_SCALAR_ATTRS: frozenset[str] = frozenset({"workspace", "github_repo"})

# Whole-function exemptions: ``(module_basename, function_name)``. The one
# deliberately non-migrated seam that still builds a client from the raw scalar.
_FUNCTION_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    # ``cmd_notify_pre_deploy`` builds
    # ``SubprocessCodeHost(repo=config.github_repo, workspace=config.workspace)``
    # in the pre-deploy-notice path — a deliberately deferred site (proposal
    # 107 out-of-scope). Exempt so the guard passes on the current tree while
    # any *new* raw-scalar construction at an enforced seam fails the build.
    ("cli.py", "cmd_notify_pre_deploy"),
})


def _raw_config_scalar_args(call: ast.Call) -> list[str]:
    """Return the ``.attr`` of every raw ``config`` scalar fed to *call*.

    Empty unless ``call`` targets a scanned constructor. For a matching call,
    walk each argument subtree — positional args and keyword values alike — and
    collect the ``.attr`` of every ``ast.Attribute`` that reads
    ``config.workspace`` / ``config.github_repo`` (attr in
    ``_RAW_SCALAR_ATTRS`` *and* value a bare ``ast.Name`` ``config``). The
    ``wt.…`` and ``config.work_target().…`` through-owner reads never match.
    """
    func = call.func
    if not (isinstance(func, ast.Name) and func.id in _CONSTRUCTOR_NAMES):
        return []
    hits: list[str] = []
    arg_nodes: list[ast.expr] = list(call.args) + [kw.value for kw in call.keywords]
    for arg in arg_nodes:
        for node in ast.walk(arg):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _RAW_SCALAR_ATTRS
                and isinstance(node.value, ast.Name)
                and node.value.id == "config"
            ):
                hits.append(node.attr)
    return hits


def _line_to_function(tree: ast.AST) -> dict[int, str]:
    """Map ``lineno`` → enclosing top-level function name within *tree*.

    Inner closures resolve to their outer named function (``ast.walk`` is
    breadth-first, so the outer ``FunctionDef`` is seen first and ``setdefault``
    keeps it) — the same shape ``tests/test_merge_truth_chokepoint.py`` uses.
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


def _violators(source: str, rel: str) -> list[str]:
    """Return ``rel:lineno — config.<attr> in <func>`` for each raw-scalar build.

    Skips any scanned-constructor call whose enclosing function is exempted in
    ``_FUNCTION_ALLOWLIST``.
    """
    tree = ast.parse(source, filename=rel)
    line_to_func = _line_to_function(tree)
    module_base = Path(rel).name
    violators: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        hits = _raw_config_scalar_args(node)
        if not hits:
            continue
        func_name = line_to_func.get(node.lineno, "<module>")
        if (module_base, func_name) in _FUNCTION_ALLOWLIST:
            continue
        for attr in hits:
            violators.append(f"{rel}:{node.lineno} — config.{attr} in {func_name}")
    return violators


# Full-AST scan of three large modules; mocking is impossible — walking the real
# AST is the test's purpose. Raise to the component budget so it stays green
# deterministically under suite load (mirrors test_merge_truth_chokepoint.py).
@pytest.mark.timeout(15)
def test_client_constructors_read_the_work_target_owner() -> None:
    """Fail when a scanned client constructor is fed a raw ``config`` scalar.

    The architectural rule: every repo-client construction seam reads the
    ``WorkTarget`` owner (``Config.work_target()`` → ``wt.repo_workspace`` /
    ``wt.github_repo``). A new construction that passes ``config.workspace`` /
    ``config.github_repo`` straight into ``SubprocessVCS`` / ``SubprocessCodeHost``
    / ``IssueTracker`` (or the ``_IssueTracker`` alias) must route through the
    owner or justify an allowlist entry — otherwise the "which repo?" fact is
    silently re-scattered.
    """
    violators: list[str] = []
    for rel in _SCANNED:
        path = _MAESTRO_ROOT / rel
        violators.extend(_violators(path.read_text(encoding="utf-8"), rel))

    assert not violators, (
        "Repo client built from a raw ``config`` scalar outside the WorkTarget "
        "owner (proposal 107):\n  "
        + "\n  ".join(violators)
        + "\n\nBuild the client from ``Config.work_target()`` "
        "(``wt.repo_workspace`` / ``wt.github_repo``) instead of "
        "``config.workspace`` / ``config.github_repo``, or — if the site is a "
        "deliberately deferred seam — add its ``(module, function)`` to "
        "``_FUNCTION_ALLOWLIST`` with a justifying comment."
    )


def test_guard_flags_a_synthetic_bypass() -> None:
    """A synthetic raw-scalar construction is detected (non-vacuity self-test).

    Drift in the AST helpers *or* in ``_violators``'s orchestration would
    silently make the guard above pass on real violators — exactly the
    regression this file exists to prevent. Route a hand-written module through
    the real ``_violators`` (not a reconstruction of its logic) and assert it
    flags exactly the raw ``config``-scalar constructions — including the
    ``_IssueTracker`` alias — while the through-owner forms are not. Exercising
    ``_violators`` itself keeps its emit branch non-vacuous: on the clean real
    tree the only hit is allowlisted, so that ``violators.append(...)`` path is
    otherwise never taken by any test.
    """
    source = (
        "def new_seam(config, wt):\n"
        "    a = SubprocessCodeHost(repo=config.github_repo, workspace=config.workspace)\n"
        "    b = _IssueTracker(config.github_repo)\n"
        "    c = SubprocessVCS(wt.repo_workspace)\n"
        "    d = IssueTracker(config.work_target().github_repo)\n"
        "    return a, b, c, d\n"
    )

    # ``new_seam`` is not allowlisted, so the raw sites reach ``_violators``'s
    # emit branch: line 2's two raw kwargs (github_repo then workspace) and
    # line 3's aliased ``_IssueTracker(config.github_repo)``. The
    # ``wt.repo_workspace`` (line 4) and ``config.work_target().github_repo``
    # (line 5) through-owner forms are not flagged.
    assert _violators(source, "evolution.py") == [
        "evolution.py:2 — config.github_repo in new_seam",
        "evolution.py:2 — config.workspace in new_seam",
        "evolution.py:3 — config.github_repo in new_seam",
    ]

    # The same raw construction inside an allowlisted ``(module, function)`` is
    # skipped by ``_violators``'s allowlist branch — proving the exemption is
    # honored through the real orchestration, not just via the clean-tree scan.
    allowlisted = (
        "def cmd_notify_pre_deploy(config):\n"
        "    return SubprocessCodeHost(repo=config.github_repo, workspace=config.workspace)\n"
    )
    assert _violators(allowlisted, "cli.py") == []


def test_scanned_constructor_modules_exist() -> None:
    """Every scanned module path resolves to a real file.

    Guards against a rename silently emptying the scan set and hiding a
    re-introduced raw-scalar construction (mirrors
    ``test_merge_truth_chokepoint.py``'s existence checks).
    """
    for rel in _SCANNED:
        assert (_MAESTRO_ROOT / rel).is_file(), f"missing scanned module: {rel}"


def test_allowlist_entries_name_real_functions() -> None:
    """Every allowlist entry must name a function that exists in its module.

    Guards against a stale exemption surviving a rename/removal and silently
    widening the hole (mirrors ``test_merge_truth_chokepoint.py``).
    """
    by_module: dict[str, set[str]] = {}
    for rel in _SCANNED:
        path = _MAESTRO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        by_module[Path(rel).name] = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    for module_base, func_name in sorted(_FUNCTION_ALLOWLIST):
        assert module_base in by_module, f"unknown module: {module_base}"
        assert func_name in by_module[module_base], (
            f"stale allowlist entry: {module_base}::{func_name}"
        )
