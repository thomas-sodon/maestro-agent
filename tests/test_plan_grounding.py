"""Unit tests for the plan-grounding owner (proposal 114, phase 1).

The owner is pure and I/O-free — :func:`confirm` decides groundedness from
*injected* text — so these build corpus/needle strings in memory and assert the
verdict plus its provenance (``grounded`` / ``category`` / ``kind``). No
filesystem, network, or subprocess is touched; no pytest fixtures and no
``mem_fs`` are needed.
"""

from __future__ import annotations

import pytest

from maestro.plan_grounding import (
    GroundingProbe,
    GroundingVerdict,
    confirm,
    planner_read_tag,
)

# ── Corpora and needles chosen so each definition shape is exercised ────────────
_DEFINED_SYMBOL = "investigation_observable_gate"

_CORPUS_DEF = (
    "import os\n\n\n"
    "def investigation_observable_gate(config):\n"
    "    return None\n"
)
_CORPUS_ASYNC_DEF = (
    "async def investigation_observable_gate(config):\n"
    "    return None\n"
)
_CORPUS_CLASS = "class investigation_observable_gate:\n    pass\n"
_CORPUS_MODULE_ASSIGN = "investigation_observable_gate = build_gate()\n"
_CORPUS_ANNOTATED_ASSIGN = "investigation_observable_gate: TaskGate = build_gate()\n"
_CORPUS_GATE_CHAIN = (
    "GATE_CHAIN = (\n"
    "    TaskGate(\n"
    '        name="investigation_observable_gate",\n'
    "        kind=\"block\",\n"
    "    ),\n"
    ")\n"
)
# A gate registered through a module-level name constant (``name=_X_GATE``).
_CORPUS_GATE_NAME_CONSTANT = '_INVESTIGATION_GATE = "investigation_observable_gate"\n'

# A corpus that merely *mentions* the name in a comment and a string literal — no
# definition-shaped occurrence, so the symbol counts as absent (grounded).
_CORPUS_BARE_MENTION = (
    "# investigation_observable_gate would be a useful gate to add\n"
    'DOC = "the investigation_observable_gate concept is described here"\n'
)

_FILE_WITH_DEFECT = "def handler():\n    result = call_without_timeout()\n    return result\n"
_DEFECT_NEEDLE = "call_without_timeout()"


def _probe(
    kind: str,
    locator: str,
    expectation: str = "",
) -> GroundingProbe:
    """Build a probe without repeating the keyword arguments at every call site."""
    return GroundingProbe(kind=kind, locator=locator, expectation=expectation)  # type: ignore[arg-type]


# ── SYMBOL_ABSENT: a defined symbol is a duplicate ─────────────────────────────


@pytest.mark.parametrize(
    "corpus",
    [
        _CORPUS_DEF,
        _CORPUS_ASYNC_DEF,
        _CORPUS_CLASS,
        _CORPUS_MODULE_ASSIGN,
        _CORPUS_ANNOTATED_ASSIGN,
        _CORPUS_GATE_CHAIN,
        _CORPUS_GATE_NAME_CONSTANT,
    ],
)
def test_symbol_absent_defined_symbol_is_ungrounded_duplicate(corpus: str) -> None:
    """A definition-shaped occurrence (def/async/class/assign/GATE_CHAIN) → duplicate."""
    verdict = confirm(_probe("SYMBOL_ABSENT", _DEFINED_SYMBOL), corpus)
    assert verdict.grounded is False
    assert verdict.category == "duplicate"
    assert verdict.kind == "SYMBOL_ABSENT"


def test_symbol_absent_missing_symbol_is_grounded() -> None:
    """A name with no definition-shaped occurrence in the corpus → grounded."""
    corpus = "def some_other_helper():\n    return 1\n"
    verdict = confirm(_probe("SYMBOL_ABSENT", _DEFINED_SYMBOL), corpus)
    assert verdict.grounded is True
    assert verdict.category is None
    assert verdict.kind == "SYMBOL_ABSENT"


def test_symbol_absent_bare_mention_is_not_a_definition() -> None:
    """A comment / string mention of the name is not a definition → grounded.

    The definition-shape guard: only anchored ``def`` / ``class`` /
    module-assignment / ``name="..."`` occurrences count, so a bare mention does
    not false-fire ``duplicate``.
    """
    verdict = confirm(_probe("SYMBOL_ABSENT", _DEFINED_SYMBOL), _CORPUS_BARE_MENTION)
    assert verdict.grounded is True
    assert verdict.category is None
    assert verdict.kind == "SYMBOL_ABSENT"


# ── DEFECT_PRESENT: the needle is the defect the plan proposes to fix ───────────


def test_defect_present_needle_present_is_grounded() -> None:
    """The expectation needle present in the file text → grounded."""
    verdict = confirm(
        _probe("DEFECT_PRESENT", "maestro/handler.py", _DEFECT_NEEDLE),
        _FILE_WITH_DEFECT,
    )
    assert verdict.grounded is True
    assert verdict.category is None
    assert verdict.kind == "DEFECT_PRESENT"


def test_defect_present_needle_absent_is_ungrounded_no_such_defect() -> None:
    """The expectation needle missing from the file text → no-such-defect."""
    clean_file = "def handler():\n    return call_with_timeout()\n"
    verdict = confirm(
        _probe("DEFECT_PRESENT", "maestro/handler.py", _DEFECT_NEEDLE),
        clean_file,
    )
    assert verdict.grounded is False
    assert verdict.category == "no-such-defect"
    assert verdict.kind == "DEFECT_PRESENT"


def test_defect_present_empty_needle_fails_safe_to_no_such_defect() -> None:
    """An empty expectation needle blocks (fail-safe), never trivially grounds.

    ``expectation`` defaults to ``""`` (unused for ``SYMBOL_ABSENT``); because
    ``"" in read_text`` is always true, a malformed ``DEFECT_PRESENT`` probe with no
    needle must *not* report grounded — the guard fails safe to ``no-such-defect``
    so a phase-2 submission guard blocks it rather than waving it through.
    """
    verdict = confirm(_probe("DEFECT_PRESENT", "maestro/handler.py"), _FILE_WITH_DEFECT)
    assert verdict.grounded is False
    assert verdict.category == "no-such-defect"
    assert verdict.kind == "DEFECT_PRESENT"


# ── LINT_PRESENT: the injected ruff findings decide the premise ─────────────────


def test_lint_present_findings_present_is_grounded() -> None:
    """Non-empty injected ruff findings ⇒ ruff reports the rule ⇒ grounded.

    The reader runs the rule against the file upstream; ``confirm`` reads only the
    injected findings text, so a non-empty read means the "fix this lint error"
    premise holds.
    """
    findings = "maestro/target.py:12:5: RUF036 `None` not at the end of a union\n"
    verdict = confirm(_probe("LINT_PRESENT", "maestro/target.py", "RUF036"), findings)
    assert verdict.grounded is True
    assert verdict.category is None
    assert verdict.kind == "LINT_PRESENT"


def test_lint_present_clean_run_is_ungrounded_no_such_lint() -> None:
    """Empty injected findings (ruff ran clean) ⇒ no-such-lint (hallucinated).

    ``""`` is the reader's signal that ruff reported nothing for the rule, so the
    claimed lint error does not exist — the guard fails to ``no-such-lint`` rather
    than grounding a plan premised on a lint error ruff does not report.
    """
    verdict = confirm(_probe("LINT_PRESENT", "maestro/target.py", "RUF036"), "")
    assert verdict.grounded is False
    assert verdict.category == "no-such-lint"
    assert verdict.kind == "LINT_PRESENT"


# ── Provenance records are frozen ──────────────────────────────────────────────


def test_probe_and_verdict_are_frozen_dataclasses() -> None:
    """The probe and verdict are small frozen provenance records."""
    probe = _probe("SYMBOL_ABSENT", _DEFINED_SYMBOL)
    verdict = confirm(probe, _CORPUS_DEF)
    assert isinstance(verdict, GroundingVerdict)
    with pytest.raises(AttributeError):
        probe.locator = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        verdict.grounded = True  # type: ignore[misc]


# ── BEHAVIOR_ABSENT — the behavioral probe that closes the name-novelty hole ────

# A live mechanism as it actually reads on disk, plus a witness substring of it.
_LIVE_MECHANISM = (
    "def _gate_pr_remediation_settled(self, item):\n"
    "    if self._is_pr_settled(item.pr_number):\n"
    "        return None\n"
    "    return item\n"
)
_LIVE_WITNESS = "self._is_pr_settled(item.pr_number)"


def test_symbol_absent_grounds_a_duplicate_under_a_novel_name() -> None:
    """The documented hole ``BEHAVIOR_ABSENT`` closes: name novelty grounds a dupe.

    ``SYMBOL_ABSENT`` asks only whether the locator is defined. A build-plan's
    locator is the name it *intends to create*, so a plan duplicating the live
    ``_gate_pr_remediation_settled`` under the fresh name
    ``_gate_pending_pr_request_settled`` is absent by construction and grounds —
    the approval is spent anyway. This pins today's behavior as the hole, not as
    a desirable outcome; ``BEHAVIOR_ABSENT`` is the kind that refutes it.
    """
    verdict = confirm(
        _probe("SYMBOL_ABSENT", "_gate_pending_pr_request_settled"),
        _LIVE_MECHANISM,
    )
    assert verdict.grounded is True
    assert verdict.category is None
    assert verdict.kind == "SYMBOL_ABSENT"


def test_behavior_absent_witness_present_is_ungrounded_duplicate() -> None:
    """A witness already present at the named site ⇒ the behavior ships ⇒ duplicate.

    The same corpus the name-novelty test grounds is refuted here, because the
    probe asks about the behavior at the site rather than the novelty of the name.
    """
    verdict = confirm(
        _probe("BEHAVIOR_ABSENT", "maestro/loop/dequeue.py", _LIVE_WITNESS),
        _LIVE_MECHANISM,
    )
    assert verdict.grounded is False
    assert verdict.category == "duplicate"
    assert verdict.kind == "BEHAVIOR_ABSENT"


def test_behavior_absent_witness_missing_is_grounded() -> None:
    """A witness absent from the named file ⇒ nothing there does this yet ⇒ grounded."""
    verdict = confirm(
        _probe("BEHAVIOR_ABSENT", "maestro/loop/dequeue.py", "not_in_this_file()"),
        _LIVE_MECHANISM,
    )
    assert verdict.grounded is True
    assert verdict.category is None
    assert verdict.kind == "BEHAVIOR_ABSENT"


def test_behavior_absent_empty_expectation_fails_safe_to_grounded() -> None:
    """An empty witness grounds — the deliberate inverse of ``DEFECT_PRESENT``.

    ``"" in text`` is always true, so a malformed probe with no witness would
    trivially refuse every to-build plan. With no witness there is nothing to
    refute the premise, so the verdict grounds and the plan falls through to the
    existing guards.
    """
    verdict = confirm(
        _probe("BEHAVIOR_ABSENT", "maestro/loop/dequeue.py", ""),
        _LIVE_MECHANISM,
    )
    assert verdict.grounded is True
    assert verdict.category is None
    assert verdict.kind == "BEHAVIOR_ABSENT"


# ── Planner-read attribution tag (proposal 143 step 5) ──────────────────────────


def test_planner_read_tag_read_when_locator_was_read() -> None:
    probe = _probe("DEFECT_PRESENT", "maestro/a.py", "needle")
    assert planner_read_tag(probe, ["maestro/b.py", "maestro/a.py"]) == " [planner-read]"


def test_planner_read_tag_unread_when_locator_not_read() -> None:
    probe = _probe("DEFECT_PRESENT", "maestro/a.py", "needle")
    assert planner_read_tag(probe, []) == " [planner-unread]"


def test_planner_read_tag_normalises_dot_slash_on_both_sides() -> None:
    probe = _probe("LINT_PRESENT", "./maestro/a.py", "E501")
    assert planner_read_tag(probe, ["maestro/a.py"]) == " [planner-read]"
    plain = _probe("BEHAVIOR_ABSENT", "maestro/a.py", "witness")
    assert planner_read_tag(plain, ["./maestro/a.py"]) == " [planner-read]"


def test_planner_read_tag_empty_when_planner_ran_blind() -> None:
    probe = _probe("DEFECT_PRESENT", "maestro/a.py", "needle")
    assert planner_read_tag(probe, None) == ""


def test_planner_read_tag_empty_for_symbol_absent() -> None:
    probe = _probe("SYMBOL_ABSENT", "maestro/a.py")
    assert planner_read_tag(probe, ["maestro/a.py"]) == ""
