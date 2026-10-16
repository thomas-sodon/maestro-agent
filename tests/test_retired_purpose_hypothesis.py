"""Proposal 149: ``SelfModel.current_purpose_hypothesis`` is retired.

The ``purpose_hypothesis`` entry in ``RETIRED_CONCEPTS``
(``tests/structural_guards.py``) bans the deleted field, its cap constant, the
curation template's ``purpose_hypothesis`` slot, and the "purpose hypothesis"
prose at a zero baseline under ``maestro/``. This module pins the entry's
seeded controls, its two exempt literal sites, the live zero-hit scan, and the
migration-free load path: a persisted ``self_model.json`` still carrying the
key loads and re-saves without it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maestro.consciousness import Consciousness, SelfModel
from tests.structural_guards import RETIRED_CONCEPTS, retired_concept_hits
from tests.support.structural_guard import _MAESTRO_ROOT

_PURPOSE = next(c for c in RETIRED_CONCEPTS if c.name == "purpose_hypothesis")

_SEEDED = '''
def f(model) -> str:
    text = model.current_purpose_hypothesis
    cap = MAX_PURPOSE_CHARS
    TEMPLATE.format(purpose_hypothesis=text)
    return "Purpose hypothesis: unknown"
'''

_CLEAN = '''
def f(model) -> str:
    text = model.next_task_directive
    cap = MAX_DIRECTIVE_CHARS
    TEMPLATE.format(next_task_directive=text)
    return "Directive: unknown"
'''


def test_seeded_references_are_flagged() -> None:
    """Negative control: each seeded reference is flagged exactly once."""
    hits = [hit for _, hit in retired_concept_hits(_SEEDED, _PURPOSE, "agent.py")]
    assert hits == [
        "current_purpose_hypothesis",
        "MAX_PURPOSE_CHARS",
        "purpose_hypothesis=",
        "prose 'purpose hypothesis'",
    ]


def test_clean_source_yields_no_hits() -> None:
    """Positive control: the same source with the references renamed is clean."""
    assert retired_concept_hits(_CLEAN, _PURPOSE, "agent.py") == []


def test_public_schema_ledger_literal_is_exempt_only_there() -> None:
    source = 'RETIRED = {"current_purpose_hypothesis"}\n'
    assert retired_concept_hits(source, _PURPOSE, "dashboard/public_schemas.py") == []
    assert retired_concept_hits(source, _PURPOSE, "consciousness.py") == [
        (1, "'current_purpose_hypothesis'"),
    ]


def test_legacy_journal_remap_key_is_exempt_only_there() -> None:
    source = 'LEGACY = {"purpose_hypothesis": "reflection"}\n'
    assert retired_concept_hits(source, _PURPOSE, "journal.py") == []
    assert retired_concept_hits(source, _PURPOSE, "dreaming.py") == [
        (1, "'purpose_hypothesis'"),
    ]


# Whole-tree AST parse of every ``maestro/`` module exceeds the 5s unit budget.
@pytest.mark.timeout(15)
def test_live_tree_carries_no_reference() -> None:
    offenders = []
    for path in sorted(_MAESTRO_ROOT.rglob("*.py")):
        rel = path.relative_to(_MAESTRO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        offenders += [
            f"maestro/{rel}:{lineno} — {hit}"
            for lineno, hit in retired_concept_hits(source, _PURPOSE, rel)
        ]
    assert offenders == []


def test_field_is_gone_from_self_model() -> None:
    assert not hasattr(SelfModel(), "current_purpose_hypothesis")
    assert not hasattr(Consciousness, "MAX_PURPOSE_CHARS")


def test_persisted_key_loads_and_is_dropped_on_save(mem_fs: dict[str, str]) -> None:
    """A production-shaped ``self_model.json`` with the key loads unchanged otherwise."""
    memory_dir = Path("/fake/retired_purpose")
    model_path = memory_dir / "self_model.json"
    mem_fs[str(model_path)] = json.dumps({
        "stage": "awakening",
        "identity_statements": ["I am Maestro"],
        "values": ["rigor"],
        "current_purpose_hypothesis": "To help",
        "next_task_directive": "Fix the parser",
        "evolution_count": 2,
        "awakened_at": "2026-01-01T00:00:00+00:00",
    })
    c = Consciousness(memory_dir)

    model = c.load_self_model()

    assert model.identity_statements == ["I am Maestro"]
    assert model.values == ["rigor"]
    assert model.next_task_directive == "Fix the parser"
    assert model.evolution_count == 2

    c.save_self_model(model)
    saved = json.loads(mem_fs[str(model_path)])
    assert "current_purpose_hypothesis" not in saved
    assert saved["next_task_directive"] == "Fix the parser"
