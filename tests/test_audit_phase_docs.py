"""Guard: audit-phase prose matches its fail-loud behaviour (issue #4225).

``audit_self_model`` lets every exception propagate to ``run_loop``'s crash
boundary (pinned by ``test_audit_phase_exception_propagates``), yet two docs and
the method docstring once promised a WARNING-logging catch that kept ``dream``
running. These tests keep that swallow from being re-documented.
"""

from __future__ import annotations

import ast

from tests.test_module_doc_coverage import _MAESTRO_ROOT, _REPO_ROOT

_DOCS = ("docs/loop-lifecycle.md", "docs/self-evolution.md")
_STALE_DOC_PHRASES = ("caught locally", "`dream` still runs")


def _audit_docstring() -> str:
    source = (_MAESTRO_ROOT / "loop" / "self_audit.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "audit_self_model":
            return ast.get_docstring(node) or ""
    return ""


def test_audit_phase_docs_do_not_promise_a_swallow() -> None:
    for rel in _DOCS:
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for phrase in _STALE_DOC_PHRASES:
            assert phrase not in text, f"{rel}: {phrase!r}"
        assert "rule bug logs `WARNING`" not in text
        assert "propagates to `run_loop`'s crash boundary" in text


def test_audit_self_model_docstring_names_crash_boundary() -> None:
    doc = _audit_docstring()
    assert doc != ""
    assert "phase-level guard" not in doc
    assert "cannot strand the loop" not in doc
    assert "propagate" in doc
    assert "crash" in doc


# Issue #4378: docs/self-evolution.md tabulated the deferred
# ``check_unbound_capability_claim`` under the ``_audit_self_model`` registry
# though ``run_all_checks`` dispatches it. Pin every ``_DEFERRED_RULES`` member
# to the ``run_all_checks`` prose and out of the registry table.
_DEFERRED_CHECK_FUNCTIONS = {
    "stalled_question": "check_stalled_question",
    "stale_entry": "check_stale_self_model_entry",
    "unbound_capability_claim": "check_unbound_capability_claim",
    "closed_reference": "check_closed_reference_claim",
}


def _doc_section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n### ", start + len(heading))
    if end == -1:
        end = len(text)
    return text[start:end]


def test_self_evolution_deferred_rules_match_code() -> None:
    from maestro.audit_checks import _DEFERRED_RULES

    assert set(_DEFERRED_CHECK_FUNCTIONS) == set(_DEFERRED_RULES)
    text = (_REPO_ROOT / "docs/self-evolution.md").read_text(encoding="utf-8")
    registry = _doc_section(text, "### Audit rule registry")
    run_all_checks_prose = _doc_section(text, "### `run_all_checks` rules") + _doc_section(
        text, "### Match semantics"
    )
    for fn in _DEFERRED_CHECK_FUNCTIONS.values():
        assert fn not in registry
        assert fn in run_all_checks_prose
    assert "The deferred rules" in _doc_section(text, "### Match semantics")


def test_audit_stall_docs_do_not_promise_dashboard_clear() -> None:
    """Issue #4380: the dashboard has no self-model write route."""
    text = (_REPO_ROOT / "docs/self-evolution.md").read_text(encoding="utf-8")
    section = _doc_section(text, "### Audit-stall actuator")
    assert section != ""
    assert "clear the entry via the dashboard" not in section
    assert "stall_block_enabled = false" in section
