"""Hard ban on retired concepts referenced in ``maestro/``.

``RETIRED_CONCEPTS`` (``tests/structural_guards.py``) owns the fact "this concept
is retired"; this module fails on any reference under ``maestro/`` outside each
concept's declared exempt sites, so new code cannot re-adopt a retired concept
(proposal 148). To retire a further concept, add a ``RetiredConcept`` to
``RETIRED_CONCEPTS``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.structural_guards import (
    RETIRED_CONCEPTS,
    RetiredConcept,
    retired_concept_hits,
)
from tests.support.structural_guard import _MAESTRO_ROOT

_CONCEPT_IDS = [c.name for c in RETIRED_CONCEPTS]
_STAGE = next(c for c in RETIRED_CONCEPTS if c.name == "evolutionary_stage")


def _live_hits(concept: RetiredConcept) -> dict[str, list[tuple[int, str]]]:
    """Scan every ``maestro/**/*.py`` for *concept*, keyed by repo-relative path."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(_MAESTRO_ROOT.rglob("*.py")):
        rel = path.relative_to(_MAESTRO_ROOT).as_posix()
        hits = retired_concept_hits(path.read_text(encoding="utf-8"), concept, rel)
        if hits:
            found[rel] = hits
    return found


# Whole-tree AST parse of every ``maestro/`` module exceeds the 5s unit budget.
@pytest.mark.timeout(15)
@pytest.mark.parametrize("concept", RETIRED_CONCEPTS, ids=_CONCEPT_IDS)
def test_no_retired_concept_reference_in_maestro(concept: RetiredConcept) -> None:
    """No module under ``maestro/`` references the concept outside its exempt sites."""
    hits = _live_hits(concept)
    offenders = [
        f"maestro/{rel}:{lineno} — {hit}"
        for rel in sorted(hits)
        for lineno, hit in hits[rel]
    ]
    assert offenders == [], (
        f"References to the retired concept {concept.name!r} (see RETIRED_CONCEPTS "
        "in tests/structural_guards.py):\n  "
        + "\n  ".join(offenders)
        + "\n\nRemove each reference, or pick a different name for an unrelated thing."
    )


_SEEDED = '''
def f(stage: str) -> None:
    journal.record_event("x", stage=model.rung)
    payload = {"stage": model.stage}
    rungs = STAGES
    return "purpose_found"
'''

_CLEAN = '''
def f(kind: str) -> None:
    journal.record_event("x", kind=model.rung)
    payload = {"kind": model.kind}
    rungs = KINDS
    return "done"
'''


def test_seeded_references_are_flagged() -> None:
    """Negative control: each seeded reference is flagged exactly once."""
    hits = [hit for _, hit in retired_concept_hits(_SEEDED, _STAGE, "agent.py")]
    assert hits == [
        "param stage", "stage=", "'stage'", "stage", "STAGES", "'purpose_found'"
    ]


_OBJECTIVE = next(c for c in RETIRED_CONCEPTS if c.name == "planning_objective")

_SEEDED_OBJECTIVE = '''
def frame(system: str, objective: Objective) -> str:
    return config.work_net_burn_objective_enabled
'''


def test_seeded_planning_objective_references_are_flagged() -> None:
    """Negative control (155 Step 3): an objective parameter and a flag read trip."""
    hits = [
        hit for _, hit in retired_concept_hits(_SEEDED_OBJECTIVE, _OBJECTIVE, "call_policy.py")
    ]
    assert "param objective" in hits
    assert "Objective" in hits
    assert "work_net_burn_objective_enabled" in hits


_REFLECT = next(c for c in RETIRED_CONCEPTS if c.name == "reflect_call")
_SEEDED_REFLECT = '''
async def evolve(engine):
    text = await engine.reflect("", caller="reflect")
    if await engine.refuses_reflected_intent(text):
        return parse_declared_intent(text)
'''


def test_seeded_reflect_references_are_flagged() -> None:
    """Negative control (155 Step 4): the reflect caller and its intent arm trip."""
    hits = [hit for _, hit in retired_concept_hits(_SEEDED_REFLECT, _REFLECT, "agent.py")]
    assert hits == ["'reflect'", "refuses_reflected_intent", "parse_declared_intent"]


def test_reflect_literal_exempt_only_in_cost_tracking() -> None:
    """The ``"reflect"`` caller survives only where cost history needs it."""
    assert [module for module, _, _ in _REFLECT.exempt_literal_sites] == ["cost_tracking.py"]


_REVENUE = next(c for c in RETIRED_CONCEPTS if c.name == "revenue_charter")
_SEEDED_REVENUE = '''
def frame(memory):
    reading = read_solvency(memory)
    note = "fund the first revenue stream"
    return CHARTER_PROMPT, reading, note
'''


def test_seeded_revenue_charter_references_are_flagged() -> None:
    """Negative control: the charter prose, a solvency read and the prose trip."""
    hits = sorted(
        hit for _, hit in retired_concept_hits(_SEEDED_REVENUE, _REVENUE, "agent.py")
    )
    assert hits == sorted(["read_solvency", "CHARTER_PROMPT", "prose 'revenue stream'"])


def test_revenue_charter_prose_spares_shell_quoting_words() -> None:
    """Bare quote / intake / charter words are not banned (only the named phrases)."""
    source = 'x = "quote the intake under the charter"\n'
    assert retired_concept_hits(source, _REVENUE, "agent.py") == []


def test_clean_source_yields_no_hits() -> None:
    """Positive control: the same source with the references removed is clean."""
    assert retired_concept_hits(_CLEAN, _STAGE, "agent.py") == []


def test_definitions_and_prose_are_flagged() -> None:
    """A retired ``def``/``class`` name and the prose phrase are each a hit."""
    source = (
        "class StageTransition: ...\n"
        "async def advance_stage(): ...\n"
        "DOC = 'The Evolutionary Stage ladder'\n"
        "x = 'unrelated'\n"
    )
    hits = [hit for _, hit in retired_concept_hits(source, _STAGE, "agent.py")]
    assert hits == [
        "def StageTransition",
        "def advance_stage",
        "prose 'evolutionary stage'",
    ]


def test_exempt_module_is_not_scanned() -> None:
    """``flag_lifecycle.py``'s unrelated ``stage`` axis is exempt; elsewhere it counts.

    Exemption is keyed by path under ``maestro/``, so a same-named module in a
    subpackage is still scanned.
    """
    source = 'CutoverFlag(stage="retain")\n'
    assert retired_concept_hits(source, _STAGE, "flag_lifecycle.py") == []
    assert retired_concept_hits(source, _STAGE, "agent.py") == [(1, "stage=")]
    assert retired_concept_hits(source, _STAGE, "loop/flag_lifecycle.py") == [
        (1, "stage=")
    ]


def test_exempt_literal_site_is_not_counted() -> None:
    """The ``self_awareness`` server name is exempt only at its declared sites."""
    source = 'name = "self_awareness"\n'
    assert retired_concept_hits(source, _STAGE, "tools.py") == []
    assert retired_concept_hits(source, _STAGE, "consciousness.py") == [
        (1, "'self_awareness'")
    ]
    assert retired_concept_hits(source, _STAGE, "loop/tools.py") == [
        (1, "'self_awareness'")
    ]


def test_live_scan_is_not_vacuous() -> None:
    """The scan walks the real tree, and a hit seeded into a real module is found."""
    modules = sorted(_MAESTRO_ROOT.rglob("*.py"))
    assert len(modules) >= 100
    real = (_MAESTRO_ROOT / "agent.py").read_text(encoding="utf-8")
    assert retired_concept_hits(real, _STAGE, "agent.py") == []
    seeded = retired_concept_hits(real + "\nx = STAGES\n", _STAGE, "agent.py")
    assert [hit for _, hit in seeded] == ["STAGES"]


def test_annotated_field_is_flagged() -> None:
    """A re-added ``stage`` field on a class is a hit, so a dataclass cannot regrow it."""
    source = 'class M:\n    stage: str = "x"\n'
    assert retired_concept_hits(source, _STAGE, "consciousness.py") == [
        (2, "field stage")
    ]


def _holds_constant(source: str, literal: str) -> bool:
    return any(
        isinstance(node, ast.Constant) and node.value == literal
        for node in ast.walk(ast.parse(source))
    )


def _module_path(module: str) -> Path:
    return _MAESTRO_ROOT / module


@pytest.mark.parametrize("concept", RETIRED_CONCEPTS, ids=_CONCEPT_IDS)
def test_exemptions_are_live(concept: RetiredConcept) -> None:
    """Every exempt module exists and every exempt literal site still holds its literal."""
    for module in sorted(concept.exempt_modules):
        assert _module_path(module).is_file(), f"exempt module {module} is gone"
    for module, literal, reason in concept.exempt_literal_sites:
        assert reason.strip(), f"exempt literal site ({module}, {literal!r}) has no reason"
        path = _module_path(module)
        assert path.is_file(), f"exempt literal site module {module} is gone"
        live = _holds_constant(path.read_text(encoding="utf-8"), literal)
        assert live, (
            f"exempt literal site ({module}, {literal!r}) matches no live "
            "constant — delete the entry"
        )


def test_registry_entries_are_well_formed() -> None:
    """Names and provenance are present; identifier/literal/keyword sets are disjoint."""
    assert len({c.name for c in RETIRED_CONCEPTS}) == len(RETIRED_CONCEPTS)
    for concept in RETIRED_CONCEPTS:
        assert concept.name.strip()
        assert concept.provenance.strip()
        assert concept.identifiers.isdisjoint(concept.literals)
        assert concept.identifiers.isdisjoint(concept.keyword_args)
        assert concept.literals.isdisjoint(concept.keyword_args)
