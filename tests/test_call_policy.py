"""Completeness and vocabulary tests for ``maestro.call_policy.CALL_POLICIES``.

The collector walks ``maestro/**/*.py`` with ``ast`` and gathers every
``caller`` tag that reaches a call seam (``QueryOptions`` / ``query`` /
``query_on_session``), so the registry cannot drift from the tree in either
direction.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import maestro.call_policy as call_policy
from maestro.call_policy import (
    CALL_POLICIES,
    EFFORT_TIERS,
    SESSION_LINEAGES,
    CallPolicy,
    EffortTier,
    SessionScope,
    single_use_call,
    tier_effort,
    tier_of,
)
from maestro.sdk import QueryOptions
from maestro.tool_surface import (
    NAMED_SURFACES,
    REFLECTION_READ_ONLY,
    REPO_MUTATING,
    RUNTIME_GROUNDED_SURFACES,
    TOOL_FREE,
    TREE_READ_ONLY,
    ToolSurface,
    effective_surface,
)

_MAESTRO_DIR = Path(__file__).resolve().parent.parent / "maestro"
_SEAMS = frozenset({"QueryOptions", "query", "query_on_session"})
_EFFORTS = frozenset({"low", "medium", "high"})
_NONE_EFFORT_CALLERS = frozenset({"execute_step", "implement_plan"})
_EVOLUTION_SINGLE_USE = frozenset({
    "propose_plan", "propose_plan_chain",
    "curate_pending_plans",
})
_SINGLE_USE = _EVOLUTION_SINGLE_USE | {
    "self_review",
}
# Shared callers that only ever run stateless (no session lineage).
_STATELESS_ONLY = frozenset({
    "respond_to_messages", "replan_for_scope",
    "startup_probe", "rate_limit_idle_probe", "warning_hold_probe",
})
# Openers whose riders are all single-use, so they declare no lineage.
_LINEAGELESS_OPENERS = frozenset({"evolution_session", "curate_session"})


_UNRESOLVED = "<unresolved>"


def _terminal_strings(node: ast.expr) -> list[str] | None:
    """Return the string values *node* can evaluate to, or ``None`` if opaque.

    Only a ``str`` constant or an ``a if c else b`` over such terminals
    resolves; any other expression (a call, attribute, f-string) is opaque.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body = _terminal_strings(node.body)
        orelse = _terminal_strings(node.orelse)
        if body is None or orelse is None:
            return None
        return body + orelse
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            consts[stmt.targets[0].id] = stmt.value.value
    return consts


def _dict_caller(node: ast.Dict) -> ast.expr | None:
    """Return the value bound to a ``"caller"`` key in a dict display."""
    for key, val in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "caller":
            return val
    return None


class _Scope:
    """One function's resolution context (not shared with nested defs)."""

    def __init__(self, func: ast.AST | None) -> None:
        self.params: set[str] = set()
        self.strings: dict[str, list[str] | None] = {}
        self.dicts: dict[str, ast.expr] = {}
        self.dict_nodes: dict[str, ast.Dict] = {}
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = func.args
            self.params = {
                arg.arg
                for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg)
                if arg is not None
            }
        if func is None:
            return
        for n in ast.walk(func):
            if isinstance(n, ast.Assign):
                targets, rhs = n.targets, n.value
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                targets, rhs = [n.target], n.value
            else:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(rhs, ast.Dict):
                    self.dict_nodes[target.id] = rhs
                    caller = _dict_caller(rhs)
                    if caller is not None:
                        self.dicts[target.id] = caller
                    continue
                values = _terminal_strings(rhs)
                prior = self.strings.get(target.id, [])
                if prior is None or values is None:
                    self.strings[target.id] = None
                else:
                    self.strings[target.id] = [*prior, *values]


def _seam_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _resolve(value: ast.expr, scope: _Scope, consts: dict[str, str]) -> list[str]:
    """Resolve a ``caller=`` value to its tags.

    Only a bare parameter of the enclosing function (a pass-through
    ``caller=caller``) is skipped; any other opaque expression resolves to
    :data:`_UNRESOLVED` so completeness fails loud rather than vacuously.
    """
    terminals = _terminal_strings(value)
    if terminals is not None:
        return terminals
    if isinstance(value, ast.Name):
        if value.id in scope.strings:
            local = scope.strings[value.id]
            return [_UNRESOLVED] if local is None else local
        if value.id in consts:
            return [consts[value.id]]
        if value.id in scope.params:
            return []
    return [_UNRESOLVED]


def _effective_kwargs(call: ast.Call, scope: _Scope) -> dict[str, ast.expr]:
    """Map keyword name → value for *call*, expanding resolvable ``**`` dicts."""
    kwargs: dict[str, ast.expr] = {}
    for kw in call.keywords:
        splat: ast.Dict | None = None
        if kw.arg is not None:
            kwargs[kw.arg] = kw.value
        elif isinstance(kw.value, ast.Dict):
            splat = kw.value
        elif isinstance(kw.value, ast.Name):
            splat = scope.dict_nodes.get(kw.value.id)
        if splat is not None:
            for key, val in zip(splat.keys, splat.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    kwargs[key.value] = val
    return kwargs


def _iter_scoped_seam_callers(
    source: str,
) -> Iterator[tuple[str, str, ast.Call, dict[str, ast.expr]]]:
    """Yield ``(qualname, tag, call, kwargs)`` for every caller tag reaching a seam.

    One recursive pass: each function's local assignments resolve
    ``caller=<name>`` and ``**<dict>`` splats inside that function (not its
    nested defs). A ``"caller"`` dict key counts only when splatted into a seam.
    *qualname* is the dotted ``ClassDef``/``FunctionDef`` chain enclosing the
    call (``""`` at module level); *kwargs* is :func:`_effective_kwargs`.
    """
    tree = ast.parse(source)
    consts = _module_constants(tree)
    out: list[tuple[str, str, ast.Call, dict[str, ast.expr]]] = []

    def visit(node: ast.AST, scope: _Scope, qualname: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{qualname}.{child.name}" if qualname else child.name
                if isinstance(child, ast.ClassDef):
                    visit(child, scope, inner)
                else:
                    visit(child, _Scope(child), inner)
                continue
            if isinstance(child, ast.Call) and _seam_name(child) in _SEAMS:
                for kw in child.keywords:
                    value: ast.expr | None = None
                    if kw.arg == "caller":
                        value = kw.value
                    elif kw.arg is None and isinstance(kw.value, ast.Dict):
                        value = _dict_caller(kw.value)
                    elif kw.arg is None and isinstance(kw.value, ast.Name):
                        value = scope.dicts.get(kw.value.id)
                    if value is not None:
                        kwargs = _effective_kwargs(child, scope)
                        out.extend(
                            (qualname, tag, child, kwargs)
                            for tag in _resolve(value, scope, consts)
                        )
            visit(child, scope, qualname)

    visit(tree, _Scope(None), "")
    yield from out


def _iter_seam_callers(source: str) -> Iterator[tuple[str, ast.Call]]:
    """Project :func:`_iter_scoped_seam_callers` onto ``(tag, call)``."""
    for _qualname, tag, call, _kwargs in _iter_scoped_seam_callers(source):
        yield tag, call


_Row = tuple[str, str, str, ast.Call, dict[str, ast.expr]]


def _scoped_rows(module: str, source: str) -> list[_Row]:
    return [
        (module, qualname, tag, call, kwargs)
        for qualname, tag, call, kwargs in _iter_scoped_seam_callers(source)
    ]


@functools.cache
def _collect_scoped_tree() -> tuple[_Row, ...]:
    rows: list[_Row] = []
    for path in sorted(_MAESTRO_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "caller" not in source:
            continue
        module = path.relative_to(_MAESTRO_DIR).as_posix()
        rows.extend(_scoped_rows(module, source))
    return tuple(rows)


def _collect_tree() -> dict[str, list[ast.Call]]:
    found: dict[str, list[ast.Call]] = {}
    for _module, _qualname, tag, node, _kwargs in _collect_scoped_tree():
        found.setdefault(tag, []).append(node)
    return found


# The flag-off ``[sdk] single_use_evolution_calls_enabled`` arms that still
# attach a single-use caller to the shared evolution session (slot B). Only
# shrinks: it empties when slot B is retired.
_SINGLE_USE_SESSION_ARMS: Counter[tuple[str, str, str]] = Counter({
    ("evolution.py", "EvolutionEngine.propose_plan", "propose_plan"): 1,
    ("evolution.py", "EvolutionEngine.propose_plan_chain",
     "propose_plan_chain"): 1,
    ("evolution.py", "EvolutionEngine.curate_pending_plans",
     "curate_pending_plans"): 1,
    # Proposal 140 declares self_review SINGLE_USE; the move off
    # implementation_session lives behind [loop]
    # self_review_own_session_enabled, so the flag-off session arm still
    # attaches here.
    ("evolution_quality.py", "self_review", "self_review"): 1,
})


def _single_use_attachments(
    rows: Iterable[_Row],
) -> Counter[tuple[str, str, str]]:
    """Count ``query_on_session`` attachments of a ``SINGLE_USE`` caller."""
    found: Counter[tuple[str, str, str]] = Counter()
    for module, qualname, tag, call, _kwargs in rows:
        policy = CALL_POLICIES.get(tag)
        if (
            _seam_name(call) == "query_on_session"
            and policy is not None
            and policy.scope is SessionScope.SINGLE_USE
            and module != "call_policy.py"
        ):
            found[(module, qualname, tag)] += 1
    return found


# Call-site ``effort=`` on ``QueryOptions``, pinned at today's live counts.
# Only shrinks: it empties when the call-site literals collapse into
# ``CALL_POLICIES`` and the guard becomes an absolute ban.
_EFFORT_LITERAL_SITES: Counter[tuple[str, str, str]] = Counter({
    # One ``base_kwargs`` dict splatted into two ``QueryOptions`` calls.
    ("agent.py", "MaestroAgent._build_contemplation_session_options",
     "contemplation_session"): 2,
    ("agent.py", "MaestroAgent._curate_pending_plans", "curate_session"): 1,
    ("agent.py", "MaestroAgent._distill_findings", "distill_findings"): 1,
    ("agent.py", "MaestroAgent._phase_evolve", "evolution_session"): 1,
    ("agent.py", "MaestroAgent._phase_generate_task",
     "task_generation_session"): 1,
    ("agent.py", "MaestroAgent._phase_review_artifacts",
     "artifact_review_session"): 1,
    ("agent.py", "MaestroAgent._probe_rate_limit", "startup_probe"): 1,
    ("agent.py", "MaestroAgent._respond_to_messages",
     "respond_to_messages"): 1,
    ("agent.py", "MaestroAgent._run_rate_limit_idle_probe",
     "rate_limit_idle_probe"): 1,
    ("agent.py", "MaestroAgent._run_warning_hold_probe",
     "warning_hold_probe"): 1,
    ("artifact_review.py", "ArtifactReviewer.review", "artifact_review"): 1,
    ("consciousness.py", "Consciousness.introspect", "introspect"): 1,
    ("consciousness.py", "Consciousness.update_self_model",
     "update_self_model"): 1,
    ("counterfactual_probe.py", "default_session_seam",
     "workspace_counterfactual"): 1,
    # No shared-session arm: the one ``QueryOptions`` literal must declare
    # the policy effort.
    ("evolution_quality.py", "_run_auto_fix_query", "auto_fix"): 1,
    ("evolution.py", "EvolutionEngine.curate_pending_plans",
     "curate_pending_plans"): 1,
    ("evolution.py", "EvolutionEngine.learn_from_rejection",
     "rejection_classifier"): 1,
    ("evolution.py", "EvolutionEngine.propose_plan", "propose_plan"): 1,
    ("evolution.py", "EvolutionEngine.propose_plan_chain",
     "propose_plan_chain"): 1,
    ("evolution_quality.py", "self_review", "self_review"): 1,
    ("loop/implementation.py", "_assess_criteria_outcome",
     "assess_criteria_outcome"): 1,
    ("loop/implementation.py", "_replan_for_scope", "replan_for_scope"): 1,
    ("loop/implementation.py", "implement_plan", "implementation_session"): 1,
    ("loop/lifecycle.py", "open_run_session", "run_session"): 1,
    ("loop/rejection_sessions.py", "rejection_sessions",
     "rejection_self_model_session"): 1,
    ("loop/rejection_sessions.py", "rejection_sessions",
     "rejection_session"): 1,
    ("loop/task_generation.py", "TaskGenerator.generate_task",
     "generate_task"): 1,
    ("planner.py", "Planner.decompose", "planner"): 1,
    ("planner.py", "Planner.replan", "replan"): 1,
})


def _effort_literal_sites(rows: Iterable[_Row]) -> Counter[tuple[str, str, str]]:
    """Count ``QueryOptions`` rows declaring ``effort`` (splats included).

    ``call_policy.py`` builds options from ``policy.effort`` and is the
    sanctioned home, so it is exempt.
    """
    found: Counter[tuple[str, str, str]] = Counter()
    for module, qualname, tag, call, kwargs in rows:
        if (
            _seam_name(call) == "QueryOptions"
            and "effort" in kwargs
            and module != "call_policy.py"
        ):
            found[(module, qualname, tag)] += 1
    return found


_Pos = tuple[int, int]


def _call_pos(call: ast.Call) -> _Pos:
    return (call.lineno, call.col_offset)


def _raw_effort_calls(source: str) -> set[_Pos]:
    """Positions of every ``QueryOptions(...)`` in *source* declaring ``effort``.

    Caller or not. ``**`` splats resolve per enclosing function exactly as
    the row collector does (:func:`_effective_kwargs`), falling back to a
    module-level dict literal.
    """
    tree = ast.parse(source)
    module_dicts = {
        stmt.targets[0].id: stmt.value
        for stmt in tree.body
        if isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and isinstance(stmt.value, ast.Dict)
    }
    found: set[_Pos] = set()

    def visit(node: ast.AST, scope: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = _Scope(child)
                inner.dict_nodes = {**module_dicts, **inner.dict_nodes}
                visit(child, inner)
                continue
            if (
                isinstance(child, ast.Call)
                and _seam_name(child) == "QueryOptions"
                and "effort" in _effective_kwargs(child, scope)
            ):
                found.add(_call_pos(child))
            visit(child, scope)

    top = _Scope(None)
    top.dict_nodes = dict(module_dicts)
    visit(tree, top)
    return found


def _unkeyed_effort_literals(
    sources: Mapping[str, str], rows: Iterable[_Row],
) -> dict[str, int]:
    """Count per module the ``effort``-declaring calls no keyed row covers.

    The row collector only yields calls whose ``caller`` resolves, so a
    caller-less ``QueryOptions(effort=...)`` would slip past the keyed pin.
    Compared per call site, not per module total, since one call with an
    ``a if c else b`` caller yields several rows.
    """
    keyed: dict[str, set[_Pos]] = {}
    for module, _qualname, _tag, call, kwargs in rows:
        if _seam_name(call) == "QueryOptions" and "effort" in kwargs:
            keyed.setdefault(module, set()).add(_call_pos(call))
    escapes: dict[str, int] = {}
    for module, source in sources.items():
        if module == "call_policy.py":
            continue
        unkeyed = _raw_effort_calls(source) - keyed.get(module, set())
        if unkeyed:
            escapes[module] = len(unkeyed)
    return escapes


_MODEL_ID_RE = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-[0-9a-z.-]+(?:\[1m\])?")
# Model-id literals outside ``model_profiles.py``. Only shrinks; empty today.
_MODEL_LITERAL_SITES: Counter[tuple[str, str]] = Counter()


def _model_id_literals(tree: ast.AST, module: str) -> Counter[tuple[str, str]]:
    """Count ``(module, qualname)`` for each string constant that *is* a model id.

    A full match keeps docstrings and strings such as
    ``@anthropic-ai/claude-code`` out.
    """
    found: Counter[tuple[str, str]] = Counter()
    if module == "model_profiles.py":
        return found

    def visit(node: ast.AST, qualname: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, f"{qualname}.{child.name}" if qualname else child.name)
                continue
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and _MODEL_ID_RE.fullmatch(child.value)
            ):
                found[(module, qualname)] += 1
            visit(child, qualname)

    visit(tree, "")
    return found


@functools.cache
def _maestro_sources() -> dict[str, str]:
    return {
        path.relative_to(_MAESTRO_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_MAESTRO_DIR.rglob("*.py"))
    }


_MISMATCH = object()


def _policy_literal(
    kwargs: dict[str, ast.expr], name: str,
) -> tuple[bool, object]:
    """Return ``(present, value)`` for kwarg *name*; opaque → :data:`_MISMATCH`."""
    if name not in kwargs:
        return False, None
    try:
        return True, ast.literal_eval(kwargs[name])
    except ValueError:
        return True, _MISMATCH


def _policy_literal_mismatches(rows: Iterable[_Row]) -> list[tuple[str, str, str]]:
    """``(module, tag, field)`` for every ``QueryOptions`` disagreeing with policy.

    An omitted kwarg means provider default, which is allowed — except on a
    ``SINGLE_USE`` caller's stateless arm, which must declare every non-``None``
    policy field so the flag-off path cannot drift from :func:`single_use_call`.
    """
    bad: list[tuple[str, str, str]] = []
    for module, _qualname, tag, call, kwargs in rows:
        policy = CALL_POLICIES.get(tag)
        if policy is None or _seam_name(call) != "QueryOptions":
            continue
        required = policy.scope is SessionScope.SINGLE_USE
        for field in ("effort",):
            present, value = _policy_literal(kwargs, field)
            if (present or required) and value != getattr(policy, field):
                bad.append((module, tag, field))
    return bad


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_every_seam_caller_is_registered() -> None:
    collected = set(_collect_tree())
    missing = collected - set(CALL_POLICIES)
    assert missing == set()


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_every_registry_key_is_still_live() -> None:
    collected = set(_collect_tree())
    stale = set(CALL_POLICIES) - collected
    assert stale == set()


def test_effort_vocabulary() -> None:
    for tag, policy in CALL_POLICIES.items():
        if tag in _NONE_EFFORT_CALLERS:
            assert policy.effort is None, tag
        else:
            assert policy.effort in _EFFORTS, tag


def test_single_use_set_is_pinned() -> None:
    single = {
        tag for tag, policy in CALL_POLICIES.items()
        if policy.scope is SessionScope.SINGLE_USE
    }
    assert single == _SINGLE_USE


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_single_use_callers_have_a_stateless_arm() -> None:
    found = _collect_tree()
    for tag in sorted(_SINGLE_USE):
        options = [n for n in found[tag] if _seam_name(n) == "QueryOptions"]
        assert options, tag


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_query_options_literals_agree_with_registry() -> None:
    assert _policy_literal_mismatches(_collect_scoped_tree()) == []


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_single_use_callers_attach_only_on_allowlisted_arms() -> None:
    """Live single-use session attachments equal the pinned flag-off arms.

    A new site (or a second attachment inside a pinned arm) would ride a
    shared session and inherit another phase's effort and transcript; a
    pinned count above the live one must be lowered so the pin only shrinks.
    """
    live = _single_use_attachments(_collect_scoped_tree())
    new = sorted((live - _SINGLE_USE_SESSION_ARMS).items(), key=str)
    stale = sorted((_SINGLE_USE_SESSION_ARMS - live).items(), key=str)
    assert new == [], (
        f"single-use caller attached to a shared session: {new} — route it "
        "through call_policy.single_use_call"
    )
    assert stale == [], f"lower stale _SINGLE_USE_SESSION_ARMS counts: {stale}"


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_effort_literals_only_on_pinned_sites() -> None:
    """Live call-site ``effort=`` literals equal the shrink-only pin."""
    live = _effort_literal_sites(_collect_scoped_tree())
    new = sorted((live - _EFFORT_LITERAL_SITES).items(), key=str)
    stale = sorted((_EFFORT_LITERAL_SITES - live).items(), key=str)
    assert new == [], (
        f"new call-site effort literal: {new} — declare the caller's effort "
        "in call_policy.CALL_POLICIES (resolved via call_policy.tier_effort)"
    )
    assert stale == [], f"lower stale _EFFORT_LITERAL_SITES counts: {stale}"


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_no_effort_literal_escapes_the_keyed_pin() -> None:
    escapes = _unkeyed_effort_literals(_maestro_sources(), _collect_scoped_tree())
    assert escapes == {}


# Parsing the whole ``maestro/`` tree once takes ~2s.
@pytest.mark.timeout(15)
def test_model_id_literals_live_only_in_model_profiles() -> None:
    live: Counter[tuple[str, str]] = Counter()
    for module, source in _maestro_sources().items():
        live += _model_id_literals(ast.parse(source), module)
    new = sorted((live - _MODEL_LITERAL_SITES).items(), key=str)
    stale = sorted((_MODEL_LITERAL_SITES - live).items(), key=str)
    assert new == [], (
        f"model-id literal outside model_profiles.py: {new} — reference "
        "model_profiles.MODEL_PROFILES / DEFAULT_MODEL instead"
    )
    assert stale == [], f"lower stale _MODEL_LITERAL_SITES counts: {stale}"


def test_pinned_single_use_examples() -> None:
    assert CALL_POLICIES["self_review"].effort == "high"
    assert CALL_POLICIES["propose_plan"] == CallPolicy(
        "high", TREE_READ_ONLY, "evolution", SessionScope.SINGLE_USE,
    )
    assert CALL_POLICIES["introspect"].surface is REFLECTION_READ_ONLY
    assert CALL_POLICIES["implementation_session"].surface is REPO_MUTATING


def test_every_policy_declares_a_named_surface() -> None:
    named = list(NAMED_SURFACES.values())
    for tag, policy in CALL_POLICIES.items():
        assert policy.surface in named, tag


def test_single_use_policies_match_evolution_slot() -> None:
    from maestro.agent import _SESSION_SLOT_PHASES

    for tag in sorted(_EVOLUTION_SINGLE_USE):
        policy = CALL_POLICIES[tag]
        assert effective_surface(tag, grounded=False) is TOOL_FREE, tag
        assert policy.phase == _SESSION_SLOT_PHASES["b"], tag


def test_propose_plan_declares_tree_read_only() -> None:
    assert CALL_POLICIES["propose_plan"].surface is TREE_READ_ONLY


def test_seeded_unregistered_literal_is_flagged() -> None:
    source = 'QueryOptions(effort="low", caller="bogus")\n'
    tags = {tag for tag, _ in _iter_seam_callers(source)}
    assert tags == {"bogus"}
    assert "bogus" not in CALL_POLICIES


def test_seeded_unregistered_constant_is_flagged() -> None:
    source = (
        'BOGUS_CALLER = "bogus_constant"\n'
        "def f(s, p):\n"
        "    return query_on_session(s, p, caller=BOGUS_CALLER)\n"
    )
    tags = {tag for tag, _ in _iter_seam_callers(source)}
    assert tags == {"bogus_constant"}
    assert "bogus_constant" not in CALL_POLICIES


def test_non_seam_and_pass_through_callers_are_ignored() -> None:
    source = (
        'CostSample(caller="telemetry_only")\n'
        "def f(s, p, caller):\n"
        "    return query_on_session(s, p, caller=caller)\n"
        "obj.x(caller='also_ignored')\n"
    )
    assert list(_iter_seam_callers(source)) == []


def test_local_conditional_assignment_resolves_both_tags() -> None:
    source = (
        "def f(s, p, priced):\n"
        '    caller = "a_tag" if priced else "b_tag"\n'
        "    return QueryOptions(caller=caller)\n"
    )
    tags = [tag for tag, _ in _iter_seam_callers(source)]
    assert tags == ["a_tag", "b_tag"]


def test_seeded_imported_constant_is_flagged() -> None:
    source = (
        "from maestro.x import IMPORTED_CALLER\n"
        "def f(s, p):\n"
        "    return query_on_session(s, p, caller=IMPORTED_CALLER)\n"
    )
    tags = {tag for tag, _ in _iter_seam_callers(source)}
    assert tags == {_UNRESOLVED}


@pytest.mark.parametrize(
    "expr", ["self._tag", 'f"x_{n}"', 'cfg.get("key", "x")', "caller_arg"],
)
def test_seeded_opaque_caller_is_flagged(expr: str) -> None:
    source = (
        "def f(self, s, p, n, cfg):\n"
        f"    caller_arg = {expr}\n"
        f"    QueryOptions(caller={expr})\n"
        "    return QueryOptions(caller=caller_arg)\n"
    )
    tags = [tag for tag, _ in _iter_seam_callers(source)]
    assert tags[-1] == _UNRESOLVED


def test_splatted_dict_caller_is_collected() -> None:
    source = (
        "def f(s):\n"
        '    base: dict[str, str] = {"effort": "high", "caller": "dict_tag"}\n'
        "    QueryOptions(**base)\n"
        '    return QueryOptions(**{"caller": "inline_tag"})\n'
    )
    tags = [tag for tag, _ in _iter_seam_callers(source)]
    assert tags == ["dict_tag", "inline_tag"]


def test_unsplatted_dict_caller_is_ignored() -> None:
    source = (
        "def f():\n"
        '    row = {"caller": "telemetry_row"}\n'
        "    return write(row)\n"
    )
    assert list(_iter_seam_callers(source)) == []


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    session = MagicMock(name="session")
    mocks: dict[str, Any] = {
        "create_session": AsyncMock(return_value=session),
        "query_on_session": AsyncMock(return_value="result"),
        "disconnect_session": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(call_policy, name, mock)
    mocks["session"] = session
    return mocks


@pytest.mark.parametrize(
    ("caller", "effort"),
    [
        ("propose_plan", "high"),
    ],
)
@pytest.mark.asyncio
async def test_single_use_call_lifecycle(
    seam: dict[str, Any], caller: str, effort: str,
) -> None:
    result = await single_use_call(caller, "hi", system="sys")
    assert result == "result"
    seam["create_session"].assert_awaited_once_with(
        system="sys",
        options=QueryOptions(
            effort=effort,
            tools=[],
            output_format=None,
            caller=caller,
        ),
        phase="evolution",
    )
    seam["query_on_session"].assert_awaited_once_with(
        seam["session"], "hi", caller=caller,
        complete_on=None, stream_idle_timeout_seconds=None,
    )
    seam["disconnect_session"].assert_awaited_once_with(seam["session"])


@pytest.mark.asyncio
async def test_single_use_call_forwards_per_call_arguments(
    seam: dict[str, Any],
) -> None:
    schema = {"type": "object"}

    def done(_msg: object, _text: str) -> bool:
        return True

    await single_use_call(
        "propose_plan", "p", system=None, output_format=schema,
        complete_on=done, stream_idle_timeout_seconds=7.5,
    )
    options = seam["create_session"].await_args.kwargs["options"]
    assert options.output_format == schema
    kwargs = seam["query_on_session"].await_args.kwargs
    assert kwargs["complete_on"] is done
    assert kwargs["stream_idle_timeout_seconds"] == 7.5


@pytest.mark.asyncio
async def test_single_use_call_disconnects_when_turn_raises(
    seam: dict[str, Any],
) -> None:
    seam["query_on_session"].side_effect = RuntimeError("turn")
    with pytest.raises(RuntimeError, match="turn"):
        await single_use_call("propose_plan", "p", system=None)
    seam["disconnect_session"].assert_awaited_once_with(seam["session"])


@pytest.mark.asyncio
async def test_single_use_call_disconnect_failure_propagates(
    seam: dict[str, Any],
) -> None:
    seam["disconnect_session"].side_effect = RuntimeError("teardown")
    with pytest.raises(RuntimeError, match="teardown"):
        await single_use_call("propose_plan", "p", system=None)


@pytest.mark.asyncio
async def test_single_use_call_refuses_shared_caller(
    seam: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="introspect"):
        await single_use_call("introspect", "p", system=None)
    seam["create_session"].assert_not_awaited()


@pytest.mark.asyncio
async def test_single_use_call_refuses_unregistered_caller(
    seam: dict[str, Any],
) -> None:
    with pytest.raises(KeyError):
        await single_use_call("bogus", "p", system=None)
    seam["create_session"].assert_not_awaited()


@pytest.mark.asyncio
async def test_single_use_call_non_tool_free_surface_keeps_tools(
    seam: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = dict(CALL_POLICIES)
    patched["propose_plan"] = CallPolicy(
        "high", REPO_MUTATING, None, SessionScope.SINGLE_USE,
    )
    monkeypatch.setattr(call_policy, "CALL_POLICIES", patched)
    await single_use_call("propose_plan", "p", system=None)
    kwargs = seam["create_session"].await_args.kwargs
    assert kwargs["options"].tools is None
    assert kwargs["phase"] is None


@pytest.mark.asyncio
async def test_single_use_call_disconnect_failure_chains_turn_error(
    seam: dict[str, Any],
) -> None:
    turn_error = RuntimeError("turn")
    seam["query_on_session"].side_effect = turn_error
    seam["disconnect_session"].side_effect = RuntimeError("teardown")
    with pytest.raises(RuntimeError, match="teardown") as excinfo:
        await single_use_call("propose_plan", "p", system=None)
    assert excinfo.value.__context__ is turn_error


def _lineage_violations(
    lineages: Mapping[str, frozenset[str]],
) -> list[tuple[str, str]]:
    """Return ``(opener, member)`` pairs breaking the lineage invariant."""
    bad: list[tuple[str, str]] = []
    for opener, members in lineages.items():
        for member in sorted(members):
            if (
                CALL_POLICIES[member].scope is not SessionScope.SHARED
                or tier_effort(member) != tier_effort(opener)
            ):
                bad.append((opener, member))
    return bad


def _surface_lineage_violations(
    lineages: Mapping[str, frozenset[str]],
) -> list[tuple[str, str, bool]]:
    """Return ``(opener, member, grounded)`` triples whose surfaces disagree.

    A turn cannot change the tools its session was opened with; grounding is
    the opener's runtime fact, so both values are tried only when the opener's
    declared surface is runtime-grounded.
    """
    bad: list[tuple[str, str, bool]] = []
    for opener, members in lineages.items():
        opener_surface = call_policy.CALL_POLICIES[opener].surface
        groundings = (
            (False, True) if opener_surface in RUNTIME_GROUNDED_SURFACES
            else (False,)
        )
        for member in sorted(members):
            for grounded in groundings:
                if effective_surface(
                    member, grounded=grounded,
                ) != effective_surface(opener, grounded=grounded):
                    bad.append((opener, member, grounded))
    return bad


def test_lineages_agree_on_surface() -> None:
    assert _surface_lineage_violations(SESSION_LINEAGES) == []


def _patch_surface(
    monkeypatch: pytest.MonkeyPatch, caller: str, surface: ToolSurface,
) -> None:
    patched = dict(CALL_POLICIES)
    patched[caller] = dataclasses.replace(patched[caller], surface=surface)
    monkeypatch.setattr(call_policy, "CALL_POLICIES", patched)


def test_seeded_tool_free_auto_fix_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_surface(monkeypatch, "auto_fix", TOOL_FREE)
    assert _surface_lineage_violations(SESSION_LINEAGES) == [
        ("implementation_session", "auto_fix", False),
    ]


def test_seeded_tool_free_introspect_is_flagged_only_grounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_surface(monkeypatch, "introspect", TOOL_FREE)
    assert _surface_lineage_violations(SESSION_LINEAGES) == [
        ("contemplation_session", "introspect", True),
    ]


def test_tier_members_are_registered_and_disjoint() -> None:
    seen: set[str] = set()
    for members in EFFORT_TIERS.values():
        assert members <= CALL_POLICIES.keys()
        assert not members & seen
        seen |= members


@pytest.mark.parametrize(
    ("caller", "effort"),
    [
        ("self_review", "high"),
        ("propose_plan", "medium"),
        ("replan_for_scope", "medium"),
        ("execute_step", "low"),
    ],
)
def test_tier_effort_pins(caller: str, effort: str) -> None:
    assert tier_effort(caller) == effort


def test_tier_of_unregistered_raises() -> None:
    with pytest.raises(KeyError):
        tier_of("bogus")


def test_tier_of_default() -> None:
    assert tier_of("generate_task") is EffortTier.DEFAULT


def test_lineage_tags_registered_and_shared() -> None:
    for opener, members in SESSION_LINEAGES.items():
        assert opener in CALL_POLICIES
        for member in members:
            assert CALL_POLICIES[member].scope is SessionScope.SHARED, member
            assert member not in _SINGLE_USE


def test_every_shared_caller_is_declared() -> None:
    members = set().union(*SESSION_LINEAGES.values())
    openers = set(SESSION_LINEAGES) | _LINEAGELESS_OPENERS
    undeclared = {
        tag for tag, policy in CALL_POLICIES.items()
        if policy.scope is SessionScope.SHARED
        and tag not in members and tag not in openers
    }
    assert undeclared == _STATELESS_ONLY


def test_lineages_agree_on_tier_effort() -> None:
    assert _lineage_violations(SESSION_LINEAGES) == []


def test_seeded_self_review_in_implementation_lineage_is_flagged() -> None:
    seeded = dict(SESSION_LINEAGES)
    seeded["implementation_session"] = (
        seeded["implementation_session"] | {"self_review"}
    )
    assert _lineage_violations(seeded) == [
        ("implementation_session", "self_review"),
    ]


def test_seeded_single_use_lineage_member_is_flagged() -> None:
    seeded = {"run_session": frozenset({"planner", "propose_plan"})}
    assert _lineage_violations(seeded) == [("run_session", "propose_plan")]


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [None, QueryOptions(hooks=None)])
async def test_single_use_call_refuses_hookless_action_gated_phase(
    seam: dict[str, Any], options: QueryOptions | None,
) -> None:
    with pytest.raises(ValueError, match="without action-gate hooks"):
        await single_use_call("self_review", "p", system=None, options=options)
    seam["create_session"].assert_not_awaited()


@pytest.mark.asyncio
async def test_single_use_call_admits_gated_phase_with_hooks(
    seam: dict[str, Any],
) -> None:
    hooks = {"PreToolUse": ["gate"]}
    supplied = QueryOptions(
        allowed_tools=["Read", "Bash"],
        mcp_servers={"fetch": {}},
        disallowed_tools=["mcp__serena__x"],
        cwd="/work",
        model="m-long",
        betas=["beta"],
        hooks=hooks,
    )
    await single_use_call("self_review", "p", system="id", options=supplied)
    seam["create_session"].assert_awaited_once_with(
        system="id",
        options=QueryOptions(
            allowed_tools=["Read", "Bash"],
            mcp_servers={"fetch": {}},
            disallowed_tools=["mcp__serena__x"],
            cwd="/work",
            model="m-long",
            betas=["beta"],
            hooks=hooks,
            effort="high",
            caller="self_review",
        ),
        phase="implementation",
    )
    seam["disconnect_session"].assert_awaited_once_with(seam["session"])


@pytest.mark.asyncio
async def test_single_use_call_options_keep_policy_tools(
    seam: dict[str, Any],
) -> None:
    await single_use_call(
        "propose_plan", "p", system=None, options=QueryOptions(cwd="/work"),
    )
    options = seam["create_session"].await_args.kwargs["options"]
    assert options.tools == []
    assert options.cwd == "/work"
    assert options.effort == "high"


@pytest.mark.parametrize(
    ("options", "match"),
    [
        (QueryOptions(effort="low", hooks={}), "effort"),
        (QueryOptions(caller="other", hooks={}), "caller"),
    ],
)
@pytest.mark.asyncio
async def test_single_use_call_refuses_disagreeing_options(
    seam: dict[str, Any], options: QueryOptions, match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        await single_use_call("self_review", "p", system=None, options=options)
    seam["create_session"].assert_not_awaited()


_PROPOSE_ARM = (
    "class EvolutionEngine:\n"
    "    async def propose_plan(self, session, p):\n"
    '        return await query_on_session(session, p, caller="propose_plan")\n'
)


def test_seeded_unpinned_attachment_is_flagged() -> None:
    source = _PROPOSE_ARM + (
        "    async def brand_new(self, session, p):\n"
        '        return await query_on_session(session, p, caller="propose_plan")\n'
    )
    live = _single_use_attachments(_scoped_rows("evolution.py", source))
    new = live - _SINGLE_USE_SESSION_ARMS
    assert new == Counter({
        ("evolution.py", "EvolutionEngine.brand_new", "propose_plan"): 1,
    })


def test_seeded_second_attachment_in_pinned_arm_is_flagged() -> None:
    source = _PROPOSE_ARM + (
        '        await query_on_session(session, p, caller="propose_plan")\n'
    )
    live = _single_use_attachments(_scoped_rows("evolution.py", source))
    new = live - _SINGLE_USE_SESSION_ARMS
    assert new == Counter({
        ("evolution.py", "EvolutionEngine.propose_plan", "propose_plan"): 1,
    })


def test_shared_caller_attachment_is_not_flagged() -> None:
    source = (
        "def f(session, p):\n"
        '    return query_on_session(session, p, caller="introspect")\n'
    )
    assert _single_use_attachments(_scoped_rows("m.py", source)) == Counter()


def test_call_policy_module_attachment_is_exempt() -> None:
    source = (
        "async def single_use_call(session, p):\n"
        '    return await query_on_session(session, p, caller="propose_plan")\n'
    )
    rows = _scoped_rows("call_policy.py", source)
    assert _single_use_attachments(rows) == Counter()


@pytest.mark.parametrize(
    ("source", "field"),
    [
        ('QueryOptions(effort="low", caller="self_review")', "effort"),
        ('QueryOptions(effort=level, caller="propose_plan")', "effort"),
        ('QueryOptions(effort="low", caller="propose_plan")', "effort"),
    ],
)
def test_seeded_policy_literal_disagreement_is_flagged(
    source: str, field: str,
) -> None:
    rows = _scoped_rows("m.py", source)
    tag = rows[0][2]
    assert _policy_literal_mismatches(rows) == [("m.py", tag, field)]


def test_omitted_and_unregistered_policy_literals_are_not_flagged() -> None:
    source = (
        'QueryOptions(caller="introspect")\n'
        'QueryOptions(effort="medium", caller="evolution_introspect")\n'
        'QueryOptions(effort="low", caller="bogus")\n'
        'query(p, effort="low", caller="self_review")\n'
    )
    assert _policy_literal_mismatches(_scoped_rows("m.py", source)) == []


@pytest.mark.parametrize(
    ("source", "fields"),
    [
        ('QueryOptions(caller="propose_plan")', ["effort"]),
        ('QueryOptions(caller="self_review")', ["effort"]),
    ],
)
def test_seeded_single_use_omitted_policy_literal_is_flagged(
    source: str, fields: list[str],
) -> None:
    rows = _scoped_rows("m.py", source)
    tag = rows[0][2]
    expected = [("m.py", tag, field) for field in fields]
    assert _policy_literal_mismatches(rows) == expected


def test_seeded_caller_less_named_splat_is_checked() -> None:
    source = (
        "def f():\n"
        '    opts = {"effort": "low"}\n'
        '    return QueryOptions(caller="introspect", **opts)\n'
    )
    rows = _scoped_rows("m.py", source)
    assert _policy_literal_mismatches(rows) == [("m.py", "introspect", "effort")]


def test_module_level_seam_has_empty_qualname() -> None:
    rows = _scoped_rows("m.py", 'QueryOptions(caller="propose_plan")\n')
    assert [(q, t) for _m, q, t, _c, _k in rows] == [("", "propose_plan")]


@pytest.mark.parametrize(
    "splat",
    [
        '    kw = {"effort": "low", "caller": "contemplation_session"}\n'
        "    return QueryOptions(**kw)\n",
        '    return QueryOptions(**{"effort": "low", '
        '"caller": "contemplation_session"})\n',
    ],
)
def test_seeded_splatted_policy_literal_disagreement_is_flagged(
    splat: str,
) -> None:
    policy = CALL_POLICIES["contemplation_session"]
    assert policy.effort != "low"
    rows = _scoped_rows("m.py", "def f():\n" + splat)
    assert _policy_literal_mismatches(rows) == [
        ("m.py", "contemplation_session", "effort"),
    ]


def test_seeded_effort_literal_in_real_evolution_is_flagged() -> None:
    source = (_MAESTRO_DIR / "evolution.py").read_text(encoding="utf-8")
    assert _effort_literal_sites(_scoped_rows("evolution.py", source)) - (
        _EFFORT_LITERAL_SITES
    ) == Counter()
    seeded = source + (
        "\n\ndef brand_new():\n"
        '    return QueryOptions(effort="high", caller="propose_plan", tools=[])\n'
    )
    live = _effort_literal_sites(_scoped_rows("evolution.py", seeded))
    assert live - _EFFORT_LITERAL_SITES == Counter({
        ("evolution.py", "brand_new", "propose_plan"): 1,
    })


def test_seeded_model_id_in_real_config_is_flagged() -> None:
    source = (_MAESTRO_DIR / "config.py").read_text(encoding="utf-8")
    assert _model_id_literals(ast.parse(source), "config.py") == Counter()
    seeded = source + '\n_FALLBACK = "claude-opus-4-8"\n'
    assert _model_id_literals(ast.parse(seeded), "config.py") == Counter({
        ("config.py", ""): 1,
    })


def test_model_id_in_model_profiles_is_exempt() -> None:
    tree = ast.parse('DEFAULT_MODEL = "claude-opus-5-5[1m]"\n')
    assert _model_id_literals(tree, "model_profiles.py") == Counter()


def test_model_id_is_keyed_by_qualname() -> None:
    tree = ast.parse('class C:\n    def f(self):\n        return "claude-haiku-4-5[1m]"\n')
    assert _model_id_literals(tree, "m.py") == Counter({("m.py", "C.f"): 1})


@pytest.mark.parametrize(
    "source",
    [
        'def f():\n    """Runs on claude-opus-4-7 by default."""\n',
        'INSTALL = "@anthropic-ai/claude-code"\n',
    ],
)
def test_model_id_mentions_are_not_flagged(source: str) -> None:
    assert _model_id_literals(ast.parse(source), "m.py") == Counter()


@pytest.mark.parametrize(
    "call",
    [
        'QueryOptions(effort="low", tools=[])',
        'QueryOptions(**{"effort": "low"})',
        'QueryOptions(**opts)',
    ],
)
def test_seeded_caller_less_effort_literal_escapes_are_flagged(call: str) -> None:
    source = f'opts = {{"effort": "low"}}\ndef f():\n    return {call}\n'
    rows = _scoped_rows("m.py", source)
    assert _unkeyed_effort_literals({"m.py": source}, rows) == {"m.py": 1}


def test_keyed_effort_literal_is_not_an_escape() -> None:
    source = 'def f():\n    return QueryOptions(effort="low", caller="propose_plan")\n'
    rows = _scoped_rows("m.py", source)
    assert _unkeyed_effort_literals({"m.py": source}, rows) == {}


def test_raw_effort_calls_ignore_other_calls_and_keys() -> None:
    source = (
        "def f(opts):\n"
        '    replace(o, effort="low")\n'
        '    QueryOptions(**{"tools": []}, **opts)\n'
    )
    assert _raw_effort_calls(source) == set()


def test_seeded_caller_less_effort_in_real_consciousness_is_flagged() -> None:
    # introspect's ``a if c else b`` caller yields two rows for one call, so a
    # per-module total would let one caller-less literal hide behind it.
    source = (_MAESTRO_DIR / "consciousness.py").read_text(encoding="utf-8")
    rows = _scoped_rows("consciousness.py", source)
    assert _unkeyed_effort_literals({"consciousness.py": source}, rows) == {}
    seeded = source + (
        "\n\ndef brand_new():\n"
        '    return QueryOptions(effort="high", tools=[])\n'
    )
    rows = _scoped_rows("consciousness.py", seeded)
    assert _unkeyed_effort_literals({"consciousness.py": seeded}, rows) == {
        "consciousness.py": 1,
    }


def test_splat_name_reused_across_functions_resolves_per_function() -> None:
    source = (
        "def f():\n"
        '    kw = {"tools": []}\n'
        "    return kw\n"
        "def g():\n"
        '    kw = {"effort": "low"}\n'
        "    return QueryOptions(**kw)\n"
    )
    rows = _scoped_rows("m.py", source)
    assert _unkeyed_effort_literals({"m.py": source}, rows) == {"m.py": 1}


def test_call_policy_module_effort_is_exempt() -> None:
    source = (
        "def single_use_call(policy):\n"
        '    return QueryOptions(effort=policy.effort, caller="propose_plan")\n'
    )
    rows = _scoped_rows("call_policy.py", source)
    assert _effort_literal_sites(rows) == Counter()
    assert _unkeyed_effort_literals({"call_policy.py": source}, rows) == {}
