"""Regression: proposal docs must not access ``tool_uses`` off ``QueryResult``.

The tool-call list on ``QueryResult`` is ``tool_calls: list[ToolCall]``
(``maestro/sdk.py``, ``class QueryResult``); the only ``tool_uses`` symbol is an
``int`` **count** on ``QueryUsage`` (``result.usage.tool_uses``). Proposal 016
(``docs/proposals/core/016-sdk-call-optimization.md``) described the stream as
``result.tool_uses`` and told an implementer to walk it as a list of tool-call
objects with ``.name`` / ``.input`` / ``.result``. Coded verbatim
(``for tool in result.tool_uses``, ``result.tool_uses[*].result``,
``_detect_tool_loop(result.tool_uses)``) that raises ``AttributeError``:
``QueryResult`` has no ``tool_uses`` member and ``QueryUsage.tool_uses`` is a
scalar, not an iterable of ``ToolCall``.

Banning the two invalid ``QueryResult``-attribute-access substrings
deterministically kills the recurrence at zero LLM cost — the same guard family
as ``tests/test_no_line_citations_in_docs.py``. The legitimate ``QueryUsage.tool_uses``
count (used by ``004-cost-aware-budgeting.md`` as ``tool_uses: int = 0``, and by
``docs/sdk-internals.md`` as ``QueryUsage.tool_uses``) matches neither substring,
so correct usages stay green.
"""

from __future__ import annotations

from pathlib import Path

_PROPOSALS_DIR = Path(__file__).resolve().parent.parent / "docs" / "proposals"

# The two invalid ``QueryResult``-attribute-access forms. ``result.tool_uses``
# (lowercase ``r``) also catches ``result.tool_uses[*].result``;
# ``QueryResult.tool_uses`` (capital ``R``) catches the dotted-class form. The
# legitimate count accesses — ``QueryUsage.tool_uses``, ``result.usage.tool_uses``,
# ``tool_uses: int`` — contain neither substring, so they do not false-match.
_BAD_TOOL_USES_FORMS = ("result.tool_uses", "QueryResult.tool_uses")


def _find_bad_tool_uses(text: str) -> list[str]:
    """Return every invalid ``tool_uses`` access form present in ``text``."""
    return [form for form in _BAD_TOOL_USES_FORMS if form in text]


def test_proposals_do_not_reference_nonexistent_queryresult_tool_uses() -> None:
    """No ``docs/proposals/**/*.md`` may access ``tool_uses`` off ``QueryResult``."""
    md_files = sorted(_PROPOSALS_DIR.rglob("*.md"))
    # Python 3.12 rglob() silently yields nothing for a missing directory
    # instead of raising, so without this guard the scan below would pass
    # vacuously (zero files checked) if docs/proposals/ ever went missing.
    assert md_files, (
        f"Expected at least one .md file under {_PROPOSALS_DIR}; "
        "directory is missing or the path is wrong, so the scan would "
        "pass without validating anything."
    )
    offenders: list[tuple[str, list[str]]] = []
    for path in md_files:
        hits = _find_bad_tool_uses(path.read_text(encoding="utf-8"))
        if hits:
            rel = path.relative_to(_PROPOSALS_DIR.parent.parent)
            offenders.append((str(rel), hits))
    assert not offenders, (
        "Proposal docs access a nonexistent tool_uses attribute on QueryResult: "
        + "; ".join(f"{rel}: {hits}" for rel, hits in offenders)
        + ". The tool-call list is QueryResult.tool_calls (list[ToolCall]); "
        "tool_uses is only an int count on QueryUsage (result.usage.tool_uses)."
    )


def test_detector_fires_on_seeded_bad_forms() -> None:
    """Negative control: the detector must catch both seeded bad forms.

    Without this, a detector regression (e.g. a typo in the banned substrings)
    would make the clean-tree assertion above pass vacuously.
    """
    text = "walk result.tool_uses and compress QueryResult.tool_uses results"
    assert _find_bad_tool_uses(text) == ["result.tool_uses", "QueryResult.tool_uses"]


def test_detector_ignores_legitimate_tool_uses_count() -> None:
    """The ``QueryUsage.tool_uses`` count access forms are allowed."""
    text = (
        "QueryUsage.tool_uses is an int; read it via result.usage.tool_uses; "
        "the field is declared tool_uses: int = 0"
    )
    assert _find_bad_tool_uses(text) == []
