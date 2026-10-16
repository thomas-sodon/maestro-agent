"""One objective statement in place of the rival framings.

``OBJECTIVE_PROMPT`` is the only system framing, it carries no revenue
wording, and it is what ``_build_identity_context`` prefixes; no priced prompt carries the
retired purpose / journey / self-evolution wording.

How the "every CALL_POLICIES prompt builder" clause is satisfied: the
static scan (``test_no_retired_phrase_in_maestro_string_literals``) walks
every string literal under ``maestro/``, so no builder -- introspect,
generate_task, execute_step, respond_to_messages or any
other caller -- can carry a retired phrase as source text.  Rendered scans
under the mocked seam additionally cover the evolution lineage
(``test_single_use_evolution_calls.CASES``) and ``update_self_model``,
where prompt text is assembled from runtime state.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from maestro.agent import MaestroAgent
from maestro.call_policy import evolution_slot_system
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.journal import Journal
from maestro.prompts.defaults import (
    _OBJECTIVE_DISCIPLINE,
    OBJECTIVE_PROMPT,
    resolve_framing_prompt,
    select_objective_prompt,
)
from tests.conftest import MockQueryResult
from tests.test_single_use_evolution_calls import (  # noqa: F401  (fixture import)
    CASES,
    FAKE_DIR,
    _Case,
    _Mocks,
    mocks,
)

_REPO = Path(__file__).resolve().parent.parent
_MAESTRO = _REPO / "maestro"

RETIRED_PHRASES = (
    "purpose hypothes",
    "journey",
    "what you will become",
    "purpose-related",
    "self-evolving",
    "nature or purpose",
    '"awareness" |',
    '| "purpose"',
)

# Exact-site allowlist: (module path relative to maestro/, stripped line text).
# artifact_review's negative instruction tells the reviewer NOT to say it.
_ALLOWED_SITES = frozenset({
    (
        "prompts/artifact_review.py",
        '- Do NOT reference "Maestro", "the agent", "self-evolving",',
    ),
})


# --- (a) objective content ---------------------------------------------------

#: Revenue-charter wording the objective must never carry again.
_REVENUE_WORDING = (
    "revenue",
    "surplus",
    "income",
    "cash floor",
    "deadline",
    "payee",
    "sole proprietorship",
    "solvency",
    "$",
    "2026-12-31",
)


def test_objective_names_its_role_without_revenue_wording() -> None:
    for needle in ("You are Maestro", "self-improving", "under human approval",
                   "Thomas for approval"):
        assert needle in OBJECTIVE_PROMPT, needle
    lowered = OBJECTIVE_PROMPT.lower()
    for phrase in _REVENUE_WORDING:
        assert phrase not in lowered, phrase
    assert OBJECTIVE_PROMPT.endswith(_OBJECTIVE_DISCIPLINE)


# --- (b) every framing seam returns the objective ---------------------------


def test_every_seam_returns_objective() -> None:
    assert select_objective_prompt() == OBJECTIVE_PROMPT
    assert evolution_slot_system("S") == f"{OBJECTIVE_PROMPT}\n\nS"
    assert resolve_framing_prompt() == OBJECTIVE_PROMPT


# --- (c) phrase scan over string literals -----------------------------------


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def scan_source(source: str, rel: str) -> list[str]:
    """Return ``rel:line: phrase`` hits for retired phrases in string literals."""
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    lines = source.splitlines()
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        lowered = node.value.lower()
        for phrase in RETIRED_PHRASES:
            if phrase not in lowered:
                continue
            for offset, text in enumerate(node.value.splitlines() or [node.value]):
                if phrase not in text.lower():
                    continue
                lineno = node.lineno + offset
                line = lines[lineno - 1].strip() if lineno <= len(lines) else text.strip()
                if (rel, line) in _ALLOWED_SITES or (rel, text.strip()) in _ALLOWED_SITES:
                    continue
                hits.append(f"{rel}:{lineno}: {phrase}")
    return hits


def test_no_retired_phrase_in_maestro_string_literals() -> None:
    hits: list[str] = []
    for path in sorted(_MAESTRO.rglob("*.py")):
        rel = path.relative_to(_MAESTRO).as_posix()
        hits.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    assert hits == []


def test_phrase_scan_fires_on_seeded_fault() -> None:
    seeded = 'X = "What change would most advance your journey?"\n'
    assert scan_source(seeded, "rival.py") == ["rival.py:1: journey"]


def test_phrase_scan_fires_inside_fstring_and_skips_docstring() -> None:
    seeded = (
        '"""A self-evolving docstring is fine."""\n'
        'def f(x):\n'
        '    """Docstring journey."""\n'
        '    return f"{x} a self-evolving agent"\n'
    )
    assert scan_source(seeded, "rival.py") == ["rival.py:4: self-evolving"]


def test_allowlisted_site_is_exempt() -> None:
    rel, line = next(iter(_ALLOWED_SITES))
    assert scan_source(f'X = """\n{line}\n"""\n', rel) == []


# --- (d) rendered priced prompts --------------------------------------------


def _assert_clean(text: str) -> None:
    lowered = text.lower()
    for phrase in RETIRED_PHRASES:
        assert phrase not in lowered, phrase


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c.caller for c in CASES])
async def test_rendered_evolution_lineage_prompts_are_clean(
    mem_fs: dict[str, str], mocks: Callable[[_Case], _Mocks], case: _Case,  # noqa: F811
) -> None:
    m = mocks(case)
    await case.invoke(True, None)
    system = m.create.call_args.kwargs["system"]
    prompt = m.seam_turn.call_args.args[1]
    assert system.startswith(OBJECTIVE_PROMPT)
    _assert_clean(system)
    _assert_clean(prompt)


@pytest.mark.asyncio
async def test_rendered_update_self_model_contract_is_clean(mem_fs: dict[str, str]) -> None:
    c = Consciousness(FAKE_DIR)
    c.load_self_model()
    query = AsyncMock(return_value=MockQueryResult(response="{}"))
    with patch("maestro.consciousness.query", query):
        await c.update_self_model("reflection", Journal(FAKE_DIR))
    prompt = query.call_args.kwargs["prompt"]
    assert "(capability / infrastructure)" in prompt
    _assert_clean(prompt)
    _assert_clean(query.call_args.kwargs.get("system") or "")


# --- (e) the identity-context seam --------------------------------------------

AGENT_DIR = Path("/fake/objective_prompt")


def _agent(**overrides: Any) -> MaestroAgent:
    from maestro.cli import _build_agent

    config = Config(
        memory_dir=AGENT_DIR,
        workspace=AGENT_DIR,
        enable_session_reuse=False,
        publication_enabled=False,
        **overrides,
    )
    return _build_agent(config)


def test_identity_context_is_objective_plus_identity(
    mem_fs: dict[str, str],
) -> None:
    agent = _agent()
    identity = agent.consciousness.get_identity_prompt()
    assert agent._build_identity_context() == f"{OBJECTIVE_PROMPT}\n\n{identity}"


def test_resolve_framing_returns_objective() -> None:
    assert resolve_framing_prompt() == OBJECTIVE_PROMPT


def test_no_evolution_slot_system_call_passes_keywords() -> None:
    """Proposal 155 Step 3: the objective keyword is gone from every call site."""
    import ast

    sites = 0
    for path in Path("maestro").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "evolution_slot_system":
                continue
            sites += 1
            assert node.keywords == [], f"{path}:{node.lineno}"
    # reflect's site went with reflect (proposal 155 step 4).
    assert sites == 5
