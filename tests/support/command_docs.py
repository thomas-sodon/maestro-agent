"""Shared helpers for the ``.claude/commands/*.md`` command-doc guards.

The command-doc test modules (``tests/test_impl_command_doc.py``,
``tests/test_plan_proposal_command_doc.py``,
``tests/test_update_proposals_command_doc.py``, …) all pin prose invariants by
slicing one named line or section out of a markdown command doc and asserting
over it. This module is the single home for those slicers, so a fix to the
slicing semantics lands once rather than in each module's private copy.
"""

from __future__ import annotations

import re


def line_containing(text: str, needle: str) -> str:
    """Slice the single line of ``text`` containing ``needle``.

    Fails loudly when the needle is absent or ambiguous: a guard that silently
    matched a *different* line than the one it was written against would assert
    over the wrong prose, so zero or multiple matches is an error rather than a
    "take the first" fallback.
    """
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"Expected exactly one line containing {needle!r}, found {len(matches)}."
    )
    return matches[0]


# A burn-in substate token immediately followed by a ``when …`` clause — the
# shape of a *restated* Status Key entry criterion, e.g. "``burn-in:ready`` when
# the observation window has not started". The three criteria live in one place,
# the ``### Status Key`` of ``docs/proposals/README.md``; a command doc that
# carries its own copy becomes a second carrier that drifts from it (the copy
# that prompted this guard had already dropped ``burn-in:graduated``'s "or the
# observation evidence is in" branch — the very branch that types proposal 111).
# Command docs may *name* the three tokens; they must point at the Status Key
# for what picks between them.
_RESTATED_CRITERION_RE = re.compile(
    r"burn-in:(?:ready|observing|graduated)`?\s+when\b"
)


def assert_no_restated_substate_criterion(doc: str, doc_name: str) -> None:
    """Assert ``doc`` names the burn-in substates without restating their criteria."""
    m = _RESTATED_CRITERION_RE.search(doc)
    assert m is None, (
        f"{doc_name} restates a burn-in substate entry criterion "
        f"({doc[m.start() : m.start() + 90]!r}…). Those criteria live only in the "
        "`### Status Key` of `docs/proposals/README.md`; name the tokens here and "
        "point at the Status Key for what picks between them, so this file cannot "
        "drift from it."
    )
