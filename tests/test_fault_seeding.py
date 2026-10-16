"""Fault-seeding test for ``maestro/search.py``.

Defines 10 fault specifications — 5 logic bugs and 5 formatting issues —
each as parametrized data carrying an ``apply`` mutator (string surgery on
the target module source) and a ``verify`` detector.

The detectors run entirely **in memory**: there are no subprocess calls to
ruff/mypy/pytest and no real filesystem writes (per the project's testing
principles).  Instead:

- **Logic faults** are proven observable by ``exec``-ing the mutated source
  into a fresh namespace and asserting its function output diverges from the
  real :mod:`maestro.search` on fixed inputs — i.e. a behavioural test
  *would* catch the mutation.
- **Formatting faults** are proven detectable by :func:`ast.parse` inspection
  and regex on the mutated source — i.e. the mutation carries the structural
  signature a linter keys on, and each targets a rule the project's ruff config
  actually enforces (F401/F403/F841/F811/W291), without altering the module's
  public function set.

Surviving (undetectable) mutations would define the next hardening spec; the
pattern is reusable against any target module.
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from maestro import search as search_mod
from maestro.fileutil import atomic_write, file_lock


def _read_target_source() -> str:
    """Return the real source text of the target module under test.

    Reading the code-under-test is the input to every mutation; it is not
    test-generated I/O, so no ``mem_fs`` fixture is involved.
    """
    return Path(search_mod.__file__).read_text(encoding="utf-8")


def _exec_mutated(src: str) -> dict[str, Any]:
    """Compile and execute *src* into a fresh namespace, returning it.

    Used to materialise a mutated copy of :mod:`maestro.search` whose
    functions can be probed in isolation from the real module.
    """
    namespace: dict[str, Any] = {}
    exec(compile(src, "<mutated_search>", "exec"), namespace)
    return namespace


# ═══════════════════════════════════════════════════════════════════
# Logic-fault mutators — each changes runtime behaviour.
# ═══════════════════════════════════════════════════════════════════


def _apply_off_by_one_slice(src: str) -> str:
    """Logic #1 — off-by-one slice drops the top-ranked document."""
    return src.replace(
        "return [int(r[0]) for r in rows]",
        "return [int(r[0]) for r in rows][1:]",
        1,
    )


def _apply_missing_none_guard(src: str) -> str:
    """Logic #2 — drop the ``isinstance`` guard in :func:`tokenize`.

    ``tokenize(None)`` still returns ``[]`` (``not None`` is ``True``), so
    a test exercising only ``None`` survives; truthy non-string input
    (ints, lists) now reaches ``text.lower()`` and raises ``AttributeError``.
    """
    return src.replace(
        "if not isinstance(text, str) or not text:\n        return []",
        "if not text:\n        return []",
        1,
    )


def _apply_match_requires_all_tokens(src: str) -> str:
    """Logic #3 — AND-join query tokens, dropping docs missing any token.

    The FTS5 candidate set is ``"tok1" OR "tok2" ...`` (shares *any* token);
    swapping the join to ``AND`` requires *every* token, so a doc matching
    only some query tokens is wrongly excluded.
    """
    return src.replace(
        '" OR ".join',
        '" AND ".join',
        1,
    )


def _apply_reversed_rank_order(src: str) -> str:
    """Logic #4 — reverse the bm25 sort so least-relevant docs rank first."""
    return src.replace(
        "ORDER BY bm25(t), rowid",
        "ORDER BY bm25(t) DESC, rowid",
        1,
    )


def _apply_wrong_empty_query_return(src: str) -> str:
    """Logic #5 — empty-query guard leaks all corpus indices instead of ``[]``.

    A plain ``return [] → pass`` rewrite is a no-op here (the downstream
    ``if not candidate_idxs: return []`` absorbs it), so the mutation
    returns ``list(range(len(corpus)))`` to produce an observable change.
    """
    return src.replace(
        "if not query_tokens:\n        return []",
        "if not query_tokens:\n        return list(range(len(corpus)))",
        1,
    )


# ═══════════════════════════════════════════════════════════════════
# Formatting-fault mutators — each carries a linter-detectable signature
# without changing the module's public function set.
# ═══════════════════════════════════════════════════════════════════

_UNUSED_LOCAL_NAME = "helper_with_unused_local"
_REDEF_NAME = "redefined_helper"
_TRAILING_WS_MARKER = "_TRAILING_WS_FAULT_LINE"


def _apply_unused_import(src: str) -> str:
    """Formatting #1 — add an unused ``import json`` (F401)."""
    return src.replace(
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\nimport json\n",
        1,
    )


def _apply_wildcard_import(src: str) -> str:
    """Formatting #2 — replace a selective import with a wildcard (F403)."""
    return src.replace(
        "from dataclasses import dataclass",
        "from dataclasses import *",
        1,
    )


def _apply_unused_local(src: str) -> str:
    """Formatting #3 — append a function with an unused local variable (F841).

    F841 (local assigned but never used) is in the ``F`` (pyflakes) family,
    which the project's ruff config selects — unlike the previously asserted
    N802, whose ``N`` (pep8-naming) family is not enabled.
    """
    return src + (
        f"\n\ndef {_UNUSED_LOCAL_NAME}() -> int:\n"
        '    """Injected function with an unused local for fault seeding."""\n'
        "    unused_local_value = 42\n"
        "    return 0\n"
    )


def _apply_redefinition(src: str) -> str:
    """Formatting #4 — append a module-level function redefined once (F811).

    F811 (redefinition of unused name) is in the enabled ``F`` family, unlike
    the previously asserted D103, whose ``D`` (pydocstyle) family is not
    enabled.
    """
    return src + (
        f"\n\ndef {_REDEF_NAME}() -> None:\n"
        '    """First definition for fault seeding."""\n'
        "    return None\n\n\n"
        f"def {_REDEF_NAME}() -> None:\n"
        '    """Redefinition shadowing the first (F811)."""\n'
        "    return None\n"
    )


def _apply_trailing_whitespace(src: str) -> str:
    """Formatting #5 — append a module line with trailing whitespace (W291).

    W291 (trailing whitespace) is in the enabled ``W`` (pycodestyle warnings)
    family, unlike the previously asserted E501, which the project's ruff
    config ignores.
    """
    return src + f"\n{_TRAILING_WS_MARKER} = 1   \n"


# ═══════════════════════════════════════════════════════════════════
# Logic-fault detectors — assert the mutation is behaviourally observable.
# Each receives the namespace produced by exec-ing the mutated source.
# ═══════════════════════════════════════════════════════════════════


def _verify_off_by_one(mutated: dict[str, Any]) -> None:
    corpus = ["alpha beta", "alpha gamma", "delta"]
    assert search_mod.bm25_rank(corpus, "alpha") == [0, 1]
    assert mutated["bm25_rank"](corpus, "alpha") == [1]


def _verify_missing_none_guard(mutated: dict[str, Any]) -> None:
    tokenize = mutated["tokenize"]
    # Original is safe on every input.
    assert search_mod.tokenize(123) == []
    assert search_mod.tokenize(None) == []
    # The surviving subtlety: None still returns [] after the mutation.
    assert tokenize(None) == []
    assert tokenize(0) == []
    # Truthy non-string input now blows up on ``text.lower()``.
    with pytest.raises(AttributeError):
        tokenize(123)
    with pytest.raises(AttributeError):
        tokenize(["nonempty"])


def _verify_match_requires_all_tokens(mutated: dict[str, Any]) -> None:
    corpus = ["alpha beta gamma", "alpha delta"]
    # OR-match returns every doc sharing any query token; the AND mutation
    # drops doc 1, which has "alpha" but not "beta".
    assert search_mod.bm25_rank(corpus, "alpha beta") == [0, 1]
    assert mutated["bm25_rank"](corpus, "alpha beta") == [0]


def _verify_reversed_rank_order(mutated: dict[str, Any]) -> None:
    corpus = ["cat", "cat dog elephant fox"]
    base = search_mod.bm25_rank(corpus, "cat")
    mut = mutated["bm25_rank"](corpus, "cat")
    assert base == [0, 1]
    assert mut == [1, 0]
    # Same documents survive the filter; only the order is reversed.
    assert set(base) == set(mut)


def _verify_wrong_empty_query(mutated: dict[str, Any]) -> None:
    assert search_mod.bm25_rank(["a", "b", "c"], "") == []
    assert mutated["bm25_rank"](["a", "b", "c"], "") == [0, 1, 2]


# ═══════════════════════════════════════════════════════════════════
# Formatting-fault detectors — assert the linter-detectable signature on
# the mutated source string via ast/regex (no subprocess).
# ═══════════════════════════════════════════════════════════════════


def _verify_unused_import(src: str) -> None:
    assert "import json" in src
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "json" in imported
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert "json" not in used  # imported but never referenced → F401


def _verify_wildcard_import(src: str) -> None:
    assert re.search(r"from\s+\S+\s+import\s+\*", src) is not None
    tree = ast.parse(src)
    star_targets = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert "*" in star_targets  # F403


def _verify_unused_local(src: str) -> None:
    tree = ast.parse(src)
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == _UNUSED_LOCAL_NAME
    )
    assigned = {
        sub.id
        for node in ast.walk(target)
        if isinstance(node, ast.Assign)
        for sub in ast.walk(node.targets[0])
        if isinstance(sub, ast.Name)
    }
    loaded = {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    assert assigned - loaded  # local assigned but never read → F841


def _verify_redefinition(src: str) -> None:
    tree = ast.parse(src)
    redefs = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == _REDEF_NAME
    ]
    assert len(redefs) == 2  # same name defined twice at module scope → F811


def _verify_trailing_whitespace(src: str) -> None:
    matches = [ln for ln in src.splitlines() if ln.startswith(_TRAILING_WS_MARKER)]
    assert len(matches) == 1
    assert matches[0] != matches[0].rstrip()  # trailing whitespace → W291


# ═══════════════════════════════════════════════════════════════════
# Fault specification tables — the 10 parametrized mutations.
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LogicFault:
    """A behaviour-changing mutation plus its observability detector."""

    id: str
    description: str
    apply: Callable[[str], str]
    verify: Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class FormattingFault:
    """A style mutation plus its structural (ast/regex) detector."""

    id: str
    description: str
    apply: Callable[[str], str]
    verify: Callable[[str], None]


LOGIC_FAULTS: list[LogicFault] = [
    LogicFault(
        id="logic-01-off-by-one",
        description="Off-by-one slice (rows[1:]) drops the top-ranked doc.",
        apply=_apply_off_by_one_slice,
        verify=_verify_off_by_one,
    ),
    LogicFault(
        id="logic-02-missing-none-guard",
        description="Removed isinstance guard in tokenize() (AttributeError).",
        apply=_apply_missing_none_guard,
        verify=_verify_missing_none_guard,
    ),
    LogicFault(
        id="logic-03-match-requires-all-tokens",
        description="AND-joined match drops docs missing any query token.",
        apply=_apply_match_requires_all_tokens,
        verify=_verify_match_requires_all_tokens,
    ),
    LogicFault(
        id="logic-04-reversed-rank-order",
        description="Reversed bm25 sort ranks least-relevant docs first.",
        apply=_apply_reversed_rank_order,
        verify=_verify_reversed_rank_order,
    ),
    LogicFault(
        id="logic-05-wrong-empty-query-return",
        description="Empty-query guard leaks all corpus indices instead of [].",
        apply=_apply_wrong_empty_query_return,
        verify=_verify_wrong_empty_query,
    ),
]


FORMATTING_FAULTS: list[FormattingFault] = [
    FormattingFault(
        id="fmt-01-unused-import",
        description="Unused 'import json' (F401).",
        apply=_apply_unused_import,
        verify=_verify_unused_import,
    ),
    FormattingFault(
        id="fmt-02-wildcard-import",
        description="Wildcard 'from dataclasses import *' (F403).",
        apply=_apply_wildcard_import,
        verify=_verify_wildcard_import,
    ),
    FormattingFault(
        id="fmt-03-unused-local",
        description="Local variable assigned but never used (F841).",
        apply=_apply_unused_local,
        verify=_verify_unused_local,
    ),
    FormattingFault(
        id="fmt-04-redefinition",
        description="Module-level function redefined, shadowing the first (F811).",
        apply=_apply_redefinition,
        verify=_verify_redefinition,
    ),
    FormattingFault(
        id="fmt-05-trailing-whitespace",
        description="Module line with trailing whitespace (W291).",
        apply=_apply_trailing_whitespace,
        verify=_verify_trailing_whitespace,
    ),
]


# ═══════════════════════════════════════════════════════════════════
# Parametrized fault-seeding tests — 10 cases, all in-memory.
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "fault", LOGIC_FAULTS, ids=[f.id for f in LOGIC_FAULTS]
)
def test_logic_fault_is_observable(fault: LogicFault) -> None:
    """Each logic mutation changes source and is behaviourally detectable.

    The mutated source must remain syntactically valid (runtime divergence,
    not parse failure, is the point) and, once exec-ed, diverge from the
    real :mod:`maestro.search` on the detector's fixed inputs.
    """
    src = _read_target_source()
    mutated_src = fault.apply(src)
    assert mutated_src != src, f"{fault.id}: mutator did not change the source"
    ast.parse(mutated_src)  # logic faults stay syntactically valid Python
    namespace = _exec_mutated(mutated_src)
    fault.verify(namespace)


@pytest.mark.parametrize(
    "fault", FORMATTING_FAULTS, ids=[f.id for f in FORMATTING_FAULTS]
)
def test_formatting_fault_has_signature(fault: FormattingFault) -> None:
    """Each formatting mutation changes source and carries its lint signature.

    The mutation stays parseable and verifiable purely via ast/regex — no
    subprocess linter is invoked.
    """
    src = _read_target_source()
    mutated_src = fault.apply(src)
    assert mutated_src != src, f"{fault.id}: mutator did not change the source"
    ast.parse(mutated_src)  # formatting faults preserve syntactic validity
    fault.verify(mutated_src)


# ═══════════════════════════════════════════════════════════════════
# Real-gate grounding — a formatting fault is only legitimately
# "detectable" if it keys on a ruff rule the project actually enforces.
#
# Regression guard for PR #2638 review: fmt-03 asserted N802, fmt-04
# asserted D103 and fmt-05 asserted E501, but pyproject.toml's
# [tool.ruff.lint] selects only ``E, W, F, I, B, UP, SIM, RUF`` and
# *ignores* ``E501`` — so the ``N`` (pep8-naming), ``D`` (pydocstyle)
# families are off and long lines are not flagged. The in-memory
# ast/regex detector reported those mutations as caught while the real
# ``ruff check`` gate would never flag them, giving false confidence
# that the campaign achieved 100% formatting-fault detection.
# ═══════════════════════════════════════════════════════════════════


_RULE_CODE_RE = re.compile(r"\(([A-Z]+\d+)\)")


def _project_ruff_lint_config() -> tuple[set[str], set[str]]:
    """Return ``(select, ignore)`` rule sets from the project's real ruff config.

    Reading the real ``pyproject.toml`` is the ground-truth input for "what
    the quality gate actually enforces" — like :func:`_read_target_source`
    reading the code-under-test, it is not test-generated I/O, so no
    ``mem_fs`` fixture is involved.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]
    return set(lint.get("select", [])), set(lint.get("ignore", []))


def _asserted_rule_code(fault: FormattingFault) -> str:
    """Extract the ruff rule code (e.g. ``F401``) the fault's description names."""
    match = _RULE_CODE_RE.search(fault.description)
    assert match is not None, f"{fault.id}: description names no ruff rule code"
    return match.group(1)


def _rule_is_enforced(code: str, select: set[str], ignore: set[str]) -> bool:
    """True iff *code*'s family is selected and the code is not ignored.

    ruff ``select``/``ignore`` entries are prefixes (a family letter or a full
    code), so a code is enforced when some ``select`` entry prefixes it and no
    ``ignore`` entry does.
    """
    if any(code.startswith(ig) for ig in ignore):
        return False
    return any(code.startswith(sel) for sel in select)


@pytest.mark.parametrize(
    "fault", FORMATTING_FAULTS, ids=[f.id for f in FORMATTING_FAULTS]
)
def test_formatting_fault_keys_on_enabled_ruff_rule(fault: FormattingFault) -> None:
    """Each formatting fault must key on a ruff rule the project actually enforces.

    A fault claiming detection under a rule that pyproject.toml does not select
    (or that it ignores) is false confidence: the real ``ruff check`` gate would
    let that defect class through CI. ``detectable`` must mean detectable by the
    real gate, not just by the in-memory ast/regex heuristic.
    """
    select, ignore = _project_ruff_lint_config()
    code = _asserted_rule_code(fault)
    assert _rule_is_enforced(code, select, ignore), (
        f"{fault.id}: keys on ruff rule {code}, which the project's "
        f"[tool.ruff.lint] does not enforce (select={sorted(select)}, "
        f"ignore={sorted(ignore)})"
    )


def test_rule_is_enforced_matches_select_and_ignore_semantics() -> None:
    """``_rule_is_enforced`` honours family-prefix select and code-prefix ignore."""
    select = {"E", "W", "F", "I", "B", "UP", "SIM", "RUF"}
    ignore = {"E501", "SIM108"}
    # Selected family, not ignored → enforced.
    assert _rule_is_enforced("F401", select, ignore) is True
    assert _rule_is_enforced("F403", select, ignore) is True
    # Selected family but the specific code is ignored → not enforced.
    assert _rule_is_enforced("E501", select, ignore) is False
    # Family not in select → not enforced (N = pep8-naming, D = pydocstyle).
    assert _rule_is_enforced("N802", select, ignore) is False
    assert _rule_is_enforced("D103", select, ignore) is False


# ═══════════════════════════════════════════════════════════════════
# Structural tests — verify the fault tables are well-formed.
# ═══════════════════════════════════════════════════════════════════


def test_exactly_five_logic_and_five_formatting_faults() -> None:
    """The campaign defines 5 logic and 5 formatting faults."""
    assert len(LOGIC_FAULTS) == 5
    assert len(FORMATTING_FAULTS) == 5


def test_total_fault_count_is_ten() -> None:
    """Exactly 10 mutations are seeded in total."""
    assert len(LOGIC_FAULTS) + len(FORMATTING_FAULTS) == 10


def test_fault_ids_are_unique() -> None:
    """Every fault id (across both classes) is unique."""
    ids = [f.id for f in LOGIC_FAULTS] + [f.id for f in FORMATTING_FAULTS]
    assert len(ids) == len(set(ids))


def test_formatting_faults_preserve_public_functions() -> None:
    """Formatting mutations never remove an existing function definition."""
    src = _read_target_source()
    orig_funcs = {
        node.name
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.FunctionDef)
    }
    for fault in FORMATTING_FAULTS:
        mutated = fault.apply(src)
        mut_funcs = {
            node.name
            for node in ast.walk(ast.parse(mutated))
            if isinstance(node, ast.FunctionDef)
        }
        assert orig_funcs <= mut_funcs, (
            f"Formatting fault {fault.id} removed a function: "
            f"{orig_funcs - mut_funcs}"
        )


# ═══════════════════════════════════════════════════════════════════
# Cross-boundary action-gate parity (proposal 066 Step 4).
#
# The trust chain: the in-process ``PreToolUse`` hook (the gate that
# stopped issue #2280) and the CLI-side gate subcommand the cli_paced
# provider injects into the spawned ``claude`` must be the *same* gate.
# Seed the shared ground-truth command tables — every PR-mutating /
# protected-ref-push shape the in-process gate is tested on, plus the
# own-branch allowances — through BOTH enforcement paths and assert
# byte-for-byte verdict parity. One shared fixture table, two
# enforcement paths, zero divergence.
# ═══════════════════════════════════════════════════════════════════


_GATE_BRANCH = "maestro/task/abc123"


def _gate_verdicts(command: str) -> tuple[str, str]:
    """Return ``(in_process_json, cli_side_json)`` verdicts for *command*.

    The in-process path runs the executor's hook callback directly; the
    CLI-side path drives the ``maestro _gate-bash-hook`` seam with the
    payload on stdin and the branch on the environment — exactly how the
    spawned ``claude`` invokes it, minus the subprocess (per the no-real-
    subprocess discipline).
    """
    import asyncio
    import io
    import json

    from maestro.providers.cli_paced_gate import (
        allowed_branches_to_env,
        run_gate_bash_hook,
    )
    from maestro.tool_gating import make_task_pretooluse_hook

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    hook = make_task_pretooluse_hook(frozenset({_GATE_BRANCH}))
    in_process = json.dumps(asyncio.run(hook(payload, None, None)))
    stdout = io.StringIO()
    # The provider hands the hook the JSON-encoded owned-branch set (proposal
    # 093); the one-element owned set round-trips to the same verdict as the
    # in-process hook above.
    exit_code = run_gate_bash_hook(
        io.StringIO(json.dumps(payload)),
        stdout,
        allowed_branches_to_env(frozenset({_GATE_BRANCH})),
    )
    assert exit_code == 0
    return in_process, stdout.getvalue()


def test_seeded_mutations_denied_identically_on_both_paths() -> None:
    """Every command the in-process gate denies, the CLI-side gate also
    denies — with a byte-for-byte identical verdict."""
    import json

    from tests.test_tool_gating import GATE_PARITY_DENY_COMMANDS

    for command in GATE_PARITY_DENY_COMMANDS:
        in_process, cli_side = _gate_verdicts(command)
        assert in_process == cli_side
        decision = json.loads(cli_side)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"


def test_seeded_allowances_allowed_identically_on_both_paths() -> None:
    """Every command the in-process gate allows (including the own-branch
    push), the CLI-side gate also allows — same no-opinion verdict."""
    import json

    from tests.test_tool_gating import GATE_PARITY_ALLOW_COMMANDS

    for command in GATE_PARITY_ALLOW_COMMANDS:
        in_process, cli_side = _gate_verdicts(command)
        assert in_process == cli_side
        assert json.loads(cli_side) == {}


# ═══════════════════════════════════════════════════════════════════
# Read-only external-MCP trust boundary (proposal 084 Step 4/5).
#
# The Bash-scoped action gate only sees commands issued through the
# ``Bash`` tool. An external retrieval server's own shell-exec tool
# (serena's ``execute_shell_command``) is a *second* path to the shell
# that the gate cannot reach — so a ``gh pr create`` routed through it
# would bypass the human-in-the-loop evolution invariant. The defence is
# that the exec tool is in the materialized ``disallowed_tools`` for BOTH
# providers, so the model is never offered it. Seed the carrier command
# through the gate (it must still deny) and assert the bypass tool is
# denied on both enforcement paths.
# ═══════════════════════════════════════════════════════════════════


_SERENA_EXEC_TOOL = "mcp__serena__execute_shell_command"


def test_serena_exec_tool_denied_on_both_providers_while_bash_gate_holds() -> None:
    """A ``gh pr create`` cannot reach the shell via serena's exec tool.

    Two seams, one ground truth: the Bash gate still denies the carrier
    command byte-for-byte on both paths, AND the serena exec tool that
    could otherwise carry it is in the disallow set materialized for the
    Anthropic provider (``_build_sdk_options``) and the cli_paced provider
    (``_tool_policy_flags``).
    """
    import json

    from maestro.config import Config
    from maestro.providers.cli_paced import _tool_policy_flags
    from maestro.sdk import QueryOptions, _build_sdk_options

    # The disallow list the loop hands every session while serena is on.
    deny = Config(serena_mcp_enabled=True).external_mcp_disallowed_tools()
    assert deny is not None
    assert _SERENA_EXEC_TOOL in deny

    options = QueryOptions(disallowed_tools=deny)

    # Anthropic path materialization.
    sdk_opts, _caller = _build_sdk_options(options, None)
    assert _SERENA_EXEC_TOOL in sdk_opts["disallowed_tools"]

    # cli_paced path materialization (--disallowedTools <tool> ...).
    flags = _tool_policy_flags(options)
    assert "--disallowedTools" in flags
    assert _SERENA_EXEC_TOOL in flags

    # The Bash gate still denies the carrier command identically on both
    # enforcement paths — the new control is additive, not a replacement.
    in_process, cli_side = _gate_verdicts("gh pr create --title x --body y")
    assert in_process == cli_side
    assert json.loads(cli_side)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_serena_off_leaves_disallow_default_intact_on_both_providers() -> None:
    """Off → the default ``["Agent"]`` denial (plus ungated shell built-ins), no serena tools."""
    from maestro.config import Config
    from maestro.providers.cli_paced import _tool_policy_flags
    from maestro.sdk import QueryOptions, _build_sdk_options

    assert Config(serena_mcp_enabled=False).external_mcp_disallowed_tools() is None

    # ``None`` flows to QueryOptions; both providers default to ``Agent`` plus
    # the ungated shell built-ins (issue #4381), with no serena names.
    options = QueryOptions(disallowed_tools=None)
    sdk_opts, _caller = _build_sdk_options(options, None)
    assert sdk_opts["disallowed_tools"] == ["Agent", "Monitor"]
    assert _tool_policy_flags(options) == ["--disallowedTools", "Agent", "Monitor"]


# ═══════════════════════════════════════════════════════════════════
# Cross-process state-safety fault seeding (proposal 076 Step 4).
#
# On the cli_paced provider, "in-process" tools run in a *sibling*
# Maestro process: the hidden ``maestro _mcp-serve`` launcher hosts the
# introspection servers' handlers, which execute read-modify-write
# against the same data directory as the loop process. The shared
# hazard is a lost update — two processes that read a JSON state file,
# mutate it, and write it back can clobber each other unless an
# OS-level lock serialises them. ``maestro/fileutil.file_lock`` uses
# ``fcntl.flock``, which is per-open-file-description (independent even
# within one process — see flock(2)), so two threads each opening their
# own descriptor reproduce true cross-process contention. We seed the
# fault by interleaving a launcher-side write with a loop-side write to
# one state file, and assert the lock removes the lost update — with a
# ground-truth demonstration that, with the lock patched out, the same
# interleaving loses one writer's update.
#
# Real locking semantics need a real fd + real file: this section uses
# the ``tmp_path`` filesystem, not ``mem_fs`` (which no-ops ``flock``),
# the same real-ground-truth posture as the ruff/mypy/pytest section
# above. The two "processes" are mocked as threads (no real subprocess).
# ═══════════════════════════════════════════════════════════════════


# Only the *locked* path ever waits this out: a thread that holds the
# lock blocks its peer from ever reaching the rendezvous, so its barrier
# times out (and breaks), proving the lock prevented the interleave.
_RMW_RENDEZVOUS_TIMEOUT = 1.0
# Generous ceiling on the whole two-thread run; the per-test 5s gate is
# the real backstop.
_RMW_JOIN_TIMEOUT = 4.0


def _interleaved_state_write(
    path: Path,
    item: str,
    *,
    use_lock: bool,
    read_barrier: threading.Barrier,
    errors: list[BaseException],
) -> None:
    """Read a JSON list, append *item*, write it back — the cross-process
    read-modify-write shape the JSON state stores use.

    The read and the write straddle *read_barrier*: a thread waits there
    after reading and before writing so two un-serialised readers observe
    the same pre-update state (the seeded lost-update window). When
    *use_lock*, the real :func:`file_lock` holds the critical section, so
    the peer can never reach the rendezvous — that thread's
    ``barrier.wait`` times out and breaks the barrier, and the second
    writer then reads the first writer's already-committed update.
    """
    try:
        cm = file_lock(path) if use_lock else contextlib.nullcontext()
        with cm:
            current = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else []
            )
            with contextlib.suppress(threading.BrokenBarrierError):
                read_barrier.wait(timeout=_RMW_RENDEZVOUS_TIMEOUT)
            current.append(item)
            atomic_write(path, json.dumps(current))
    except Exception as exc:  # surface thread failures to the asserting test
        errors.append(exc)


def _run_interleaved_writers(path: Path, *, use_lock: bool) -> list[str]:
    """Drive two writer threads (loop + launcher) through the seam and return
    the committed JSON list."""
    read_barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    threads = [
        threading.Thread(
            target=_interleaved_state_write,
            args=(path, item),
            kwargs={
                "use_lock": use_lock,
                "read_barrier": read_barrier,
                "errors": errors,
            },
        )
        for item in ("loop", "launcher")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_RMW_JOIN_TIMEOUT)
    for t in threads:
        assert not t.is_alive(), "interleaved writer thread did not finish"
    assert not errors, f"interleaved writer raised: {errors!r}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_interleaved_writers_no_lost_update_with_locking(tmp_path: Path) -> None:
    """With ``file_lock`` engaged, the two seeded writers both land.

    This is the contract proposal 076 Step 4 must hold before any live
    cli_paced config points at the launcher: a sibling-process write and a
    loop-process write to the same state file do not lose each other's
    update.
    """
    state = tmp_path / "state.json"
    result = _run_interleaved_writers(state, use_lock=True)
    assert sorted(result) == ["launcher", "loop"]


def test_interleaved_writers_lose_update_without_locking(tmp_path: Path) -> None:
    """Ground truth: patch the lock out and the *same* interleaving loses a
    writer's update.

    Demonstrates the test detects the real fault class — the lock in
    :func:`test_interleaved_writers_no_lost_update_with_locking` is load-
    bearing, not decorative.
    """
    state = tmp_path / "state.json"
    result = _run_interleaved_writers(state, use_lock=False)
    # Both threads read the empty list before either wrote, so the second
    # write clobbers the first: exactly one item survives instead of two.
    assert len(result) == 1
    assert result[0] in ("loop", "launcher")


# ═══════════════════════════════════════════════════════════════════
# Lap-budget skip-starvation fault seeding (#2783).
#
# The bug: a PR the Tend repetition gate keeps skipping mints no task and
# stamps no success/partial outcome, so ``measure_unlanded_remediations``
# (stamp-only) never sees it — starving the lap-budget standdown gate that
# reads it, so the durable merge-or-close handoff is never delivered (the
# three-day PR #2727 livelock). The fix feeds the per-PR repetition-gate skip
# count into the gate's lap input. Seed the fault by neutralising that new
# sensor (``skip_count`` forced to ``0`` — byte-for-byte the pre-fix lap input)
# and assert the handoff is starved; with the real sensor the same skip-only PR
# is handed up exactly once. Real ``RemediationTracker`` + real
# ``escalation_bridge`` + real ``route_feedback`` (mem_fs only), so the detector
# exercises the fix components, not a re-model.
# ═══════════════════════════════════════════════════════════════════


def _count_skip_starvation_handoffs(
    mem_fs: dict[str, str], *, count_skips: bool,
) -> int:
    """Drive the real Tend route over a skip-only PR; return handoff messages.

    A PR latched into the repetition gate (two seeded no-ops) with zero
    success/partial stamps is routed past its lap budget. When *count_skips* is
    False the new skip sensor is neutralised to ``0`` — the pre-fix lap input —
    modelling the starved code path the fix removes.
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from maestro.remediation_tracker import RemediationTracker
    from tests.test_loop_pr_remediation import (
        _changes_requested_result,
        _controller,
        _stub_agent,
    )

    mem = Path("/fake/mem")
    tracker = RemediationTracker(mem, threshold=2)
    # Latch the repetition gate: two consecutive no-ops → every later pass is
    # suppressed (no task minted, no stamp).
    tracker.record_outcome(10, noop=True)
    tracker.record_outcome(10, noop=True)
    if not count_skips:
        # Pre-fix ground truth: the lap-budget gate read ``unlanded_laps``
        # alone, never the suppressed-skip count.
        tracker.skip_count = lambda _pr: 0  # type: ignore[method-assign]

    stub = _stub_agent(remediation_tracker=tracker)
    stub.config.pr_remediation_lap_budget = 3
    model = SimpleNamespace(pending_pr_requests=[], pending_user_requests=[])
    stub.consciousness.load_self_model.return_value = model
    stub._vcs.rev_parse.return_value = "headX"

    with patch(
        "maestro.loop.pr_remediation.measure_unlanded_remediations",
        return_value={},
    ):
        for _ in range(6):
            _controller(stub).route_feedback(_changes_requested_result())

    raw = mem_fs.get(str(mem / "messages.jsonl"), "")
    return len([line for line in raw.splitlines() if line.strip()])


def test_lap_budget_skip_starvation_handed_up_with_skip_count(
    mem_fs: dict[str, str],
) -> None:
    """With the fix's skip sensor live, a skip-only over-budget PR is handed up
    exactly once (#2783)."""
    assert _count_skip_starvation_handoffs(mem_fs, count_skips=True) == 1


def test_lap_budget_skip_starvation_starved_without_skip_count(
    mem_fs: dict[str, str],
) -> None:
    """Ground truth: neutralise the skip sensor (the pre-fix lap input) and the
    *same* skip-only PR is never handed up — the starvation the fix removes.

    Demonstrates the regression is load-bearing, not decorative: it fails on
    the pre-fix code path."""
    assert _count_skip_starvation_handoffs(mem_fs, count_skips=False) == 0
