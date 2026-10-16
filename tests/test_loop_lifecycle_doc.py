"""Regression (#4083): the fail-loud docs cite only real rate-limit symbols.

``docs/loop-lifecycle.md`` § *Loud Loop Phase Failures* once sanctioned an
``except RateLimitError`` in ``contemplate`` driving an ``iteration_failed`` /
``rate_limit_resets_at`` backoff, and ``docs/sdk-internals.md`` repeated that
vocabulary — none of it exists in ``maestro/``. The handler that actually
lives in the dispatch body is ``run_loop``'s ``except SDKError``. These tests
keep the dead names out while ``maestro/`` defines none of them, and pin the
section to the real handler and to ``_LOOP_PHASES`` instead of a phase count.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOOP_LIFECYCLE_DOC = _REPO_ROOT / "docs" / "loop-lifecycle.md"
_SDK_INTERNALS_DOC = _REPO_ROOT / "docs" / "sdk-internals.md"
_MAESTRO_PKG = _REPO_ROOT / "maestro"

_SECTION_HEADER = "### Loud Loop Phase Failures"

_DEAD_SYMBOLS = ("RateLimitError", "iteration_failed", "rate_limit_resets_at")

_REQUIRED_IN_SECTION = (
    "SDKError",
    "STALE_SESSION",
    "STREAM_STALL",
    "CLI_CRASH",
    "RATE_LIMIT",
    "_MAX_CONSECUTIVE_RATE_LIMITS",
)


def _section_body(doc: str) -> str:
    """Return the section body, up to the next heading of level 3 or higher."""
    start = doc.index(_SECTION_HEADER) + len(_SECTION_HEADER)
    match = re.compile(r"^#{1,3} ", re.MULTILINE).search(doc, start)
    if match is None:
        return doc[start:]
    return doc[start : match.start()]


def _defined_names(tree: ast.AST) -> set[str]:
    """Class, function, and assignment-target names defined in *tree*."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _maestro_defined_names() -> set[str]:
    names: set[str] = set()
    for path in _MAESTRO_PKG.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        # A definition must spell the name, so skipping files that mention no
        # dead symbol keeps this scan well under the unit timeout.
        if any(symbol in source for symbol in _DEAD_SYMBOLS):
            names |= _defined_names(ast.parse(source))
    return names


def test_defined_names_sees_definitions_not_string_literals() -> None:
    tree = ast.parse(
        "class A: pass\n"
        "def b(c): pass\n"
        "d = 1\n"
        "self.e = 2\n"
        "x = {'RateLimitError': 1}\n"
    )
    names = _defined_names(tree)
    assert {"A", "b", "c", "d", "e", "x"} <= names
    assert "RateLimitError" not in names


def test_loud_failures_section_names_no_dead_rate_limit_symbols() -> None:
    defined = _maestro_defined_names()
    docs = {
        path.name: path.read_text(encoding="utf-8")
        for path in (_LOOP_LIFECYCLE_DOC, _SDK_INTERNALS_DOC)
    }
    stale = [
        (name, symbol)
        for symbol in _DEAD_SYMBOLS
        if symbol not in defined
        for name, text in docs.items()
        if symbol in text
    ]
    assert stale == []


def test_loud_failures_section_names_sdk_error_handler() -> None:
    body = _section_body(_LOOP_LIFECYCLE_DOC.read_text(encoding="utf-8"))
    missing = [token for token in _REQUIRED_IN_SECTION if not _names_token(token, body)]
    assert missing == []


def _names_token(token: str, body: str) -> bool:
    """Whole-word match: ``_MAX_CONSECUTIVE_RATE_LIMITS`` must not satisfy ``RATE_LIMIT``."""
    return re.search(rf"\b{re.escape(token)}\b", body) is not None


def test_names_token_rejects_substring_of_longer_constant() -> None:
    assert _names_token("RATE_LIMIT", "the `_MAX_CONSECUTIVE_RATE_LIMITS` ceiling") is False
    assert _names_token("RATE_LIMIT", "`RATE_LIMIT` sleeps") is True


def test_loud_failures_section_has_no_hardcoded_phase_count() -> None:
    body = _section_body(_LOOP_LIFECYCLE_DOC.read_text(encoding="utf-8"))
    assert re.findall(r"\b\d+-phase\b", body) == []
    assert "_LOOP_PHASES" in body


def test_section_body_stops_at_next_heading() -> None:
    doc = f"{_SECTION_HEADER}\n\nkept\n\n### Next\n\ndropped\n"
    assert _section_body(doc) == "\n\nkept\n\n"


def test_section_body_runs_to_end_without_next_heading() -> None:
    doc = f"{_SECTION_HEADER}\n\nkept\n#### Sub\nalso kept\n"
    assert _section_body(doc) == "\n\nkept\n#### Sub\nalso kept\n"
