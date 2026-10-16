"""Regression: sdk-internals.md documents the ``bypassPermissions`` trade-off.

Every SDK call Maestro makes runs with ``permission_mode="bypassPermissions"``
(issue #410): the loop is unattended, so a blocked command would crash it, and
under this mode the CLI short-circuits the permission flow before any
``can_use_tool`` callback fires — the only working enforcement mechanism is a
``PreToolUse`` hook (``maestro/tool_gating.py``, #2280), which gates exactly
the PR/push-bypass class while general shell allowlisting stays deferred to
security proposals 024/025/026. That accepted trade-off must be discoverable
in ``docs/sdk-internals.md``, the canonical operator-facing home for SDK
behavior. This test pins the section in place: a future edit cannot silently
drop the accepted-risk statement from the doc.

Every keyword assertion is scoped to the section *body* (header to next
``## `` heading), not the whole doc: ``PreToolUse`` and ``tool_gating.py``
already appear in other sections, and a bare ``024`` matches the doc's
``>= 1024`` validation sentence — doc-wide substring checks would keep
passing after the section body was gutted.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_INTERNALS_DOC = _REPO_ROOT / "docs" / "sdk-internals.md"

_SECTION_HEADER = "## Permission mode and action gating"

# Load-bearing facts the section must state, asserted within the section
# body only. Proposal references use their parenthesized descriptions so a
# stray three-digit number elsewhere in the section cannot satisfy them.
_REQUIRED_IN_SECTION = (
    # The mode itself, and the callback it silences.
    "bypassPermissions",
    "can_use_tool",
    # The one enforcement mechanism that still fires, and its implementation.
    "PreToolUse",
    "tool_gating.py",
    # Deferred general shell allowlisting — the accepted-risk statement.
    "024 (diff danger-pattern gate)",
    "025 (implement-phase tool gating)",
    "026 (sandbox container)",
    # Audit-trail claims must cite real symbols: the tool-call count lives on
    # the per-query ``QueryUsage`` (``IterationUsage`` has no such field and
    # nothing writes it to ``usage.jsonl``).
    "QueryUsage.tool_uses",
)


def _section_body(doc: str) -> str:
    """Return the section's body text, from its header to the next ``## ``."""
    start = doc.index(_SECTION_HEADER) + len(_SECTION_HEADER)
    end = doc.find("\n## ", start)
    if end == -1:
        return doc[start:]
    return doc[start:end]


def test_permission_mode_tradeoff_documented() -> None:
    doc = _SDK_INTERNALS_DOC.read_text(encoding="utf-8")
    assert _SECTION_HEADER in doc, (
        "docs/sdk-internals.md must carry a '## Permission mode and action "
        "gating' section so the bypassPermissions trade-off is discoverable "
        "by an operator reading the SDK docs."
    )
    section = _section_body(doc)
    for required in _REQUIRED_IN_SECTION:
        assert required in section, (
            f"The '{_SECTION_HEADER}' section of docs/sdk-internals.md must "
            f"contain {required!r}; a match elsewhere in the doc does not "
            "count — the accepted-risk statement has to live in this section."
        )
