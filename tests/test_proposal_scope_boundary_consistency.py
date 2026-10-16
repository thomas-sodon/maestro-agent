"""Regression: a proposal's End-state narrative must not contradict its Out of
scope boundary on throttle/plan-headroom consumption.

The concrete failure this test kills (#2423): proposal
``core/062-cli-paced-slash-command-metrics.md`` describes a ``cli_paced`` probe
that surfaces ``/usage`` plan rate-limit headroom as a *read-only* signal the
throttle *may consult*. Step 5, the ``## Out of scope`` section, and the shipped
code all agree on that boundary — headroom is recorded and exposed into the
burn-rate throttle's *entry report* only; the throttle's gating math never
consults it, and wiring it in is deferred to proposal 049. The lone outlier was
the End-state paragraph, which read the probe "threads plan-headroom into the
throttle's awareness" — active-consumption phrasing that implies the throttle
module was changed to consume the signal, exactly the wiring the Out-of-scope
section defers. An implementer skimming only the End-state paragraph would build
throttle-gating logic the proposal explicitly excludes.

The guard fires only on the self-contradiction: an End-state narrative that uses
active-consumption phrasing about the throttle and plan-headroom *while* the same
proposal's ``## Out of scope`` section defers wiring headroom into the throttle's
gating math. Read-only framing ("surfaces plan-headroom as a read-only signal the
throttle can consult") is silent, even alongside the same deferral. Scoped to
``docs/proposals/core/``. Same read-only checked-in-docs meta-test pattern as the
sibling ``tests/test_no_line_citations_in_docs.py`` and
``tests/test_proposal_no_stale_missing_section.py`` — no ``mem_fs``, no network,
no subprocess.
"""

from __future__ import annotations

import re
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent.parent / "docs" / "proposals" / "core"

# Captures the End-state narrative: the ``End state`` lead-in (a paragraph
# ``End state:`` prefix or an ATX ``## End state`` header) through to the end of
# that first paragraph or the next ATX header — whichever comes first. DOTALL
# lets the body span the wrapped lines; MULTILINE anchors the boundaries;
# IGNORECASE tolerates casing drift on the lead-in.
_END_STATE = re.compile(
    r"(?:^#{1,6}[ \t]+End[ \t]state\b[ \t]*:?|^\**End[ \t]state\**[ \t]*:)"
    r"(?P<body>.*?)"  # the End-state narrative (non-greedy)
    r"(?=\n[ \t]*\n|^#{1,6}[ \t]|\Z)",  # blank line or next ATX header or EOF
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# Captures the ``## Out of scope`` section body, ``##``-level header only, up to
# the next ATX header or EOF (mirrors the sibling missing-section capture).
_OUT_OF_SCOPE = re.compile(
    r"^##[ \t]+Out of scope[ \t]*$"
    r"(?P<body>.*?)"
    r"(?=^#{1,6}[ \t]|\Z)",
    re.MULTILINE | re.DOTALL,
)

# End-state signal: the throttle actively becomes aware of / consumes plan
# headroom. ``plan-headroom into the throttle`` (the shape #2423 shipped, whose
# ``\s+`` spans the wrapped newline), ``throttle's awareness of headroom``, or
# ``throttle consumes/consuming headroom``. The read-only phrasing "surfaces
# plan-headroom as a read-only signal the throttle can consult" matches none of
# these ("as a read-only signal" not "into", "can consult" not "consumes").
_ACTIVE_CONSUMPTION = re.compile(
    r"plan-?headroom\s+into\s+the\s+throttle"
    r"|throttle(?:'?s)?\s+awareness\s+of\s+(?:plan-?)?headroom"
    r"|throttle\s+cons(?:umes|uming)\s+(?:plan-?)?headroom",
    re.IGNORECASE,
)

# Out-of-scope signal: wiring plan (rate-limit) headroom into the throttle's
# gating math is deferred. ``[^.]*`` bridges "'s actual " between "throttle" and
# "gating math" without crossing a sentence boundary.
_THROTTLE_GATING_DEFERRAL = re.compile(
    r"headroom\s+into\s+the\s+throttle(?:'?s)?[^.]*gating\s+math",
    re.IGNORECASE,
)


def _find_scope_boundary_contradictions(text: str) -> list[str]:
    """Return the End-state body of ``text`` iff it promises active throttle
    consumption of plan-headroom while the Out-of-scope section defers wiring
    headroom into the throttle's gating math (possibly empty)."""
    end_state = _END_STATE.search(text)
    out_of_scope = _OUT_OF_SCOPE.search(text)
    if end_state is None or out_of_scope is None:
        return []
    end_state_body = end_state.group("body")
    out_of_scope_body = out_of_scope.group("body")
    if _ACTIVE_CONSUMPTION.search(end_state_body) and _THROTTLE_GATING_DEFERRAL.search(
        out_of_scope_body
    ):
        return [end_state_body.strip()]
    return []


def test_no_scope_boundary_contradiction_in_core_proposals() -> None:
    """No ``core/**/*.md`` proposal may promise throttle consumption of plan
    headroom in its End state while deferring that wiring in Out of scope."""
    md_files = sorted(_CORE_DIR.rglob("*.md"))
    # Python 3.12 rglob() silently yields nothing for a missing directory
    # instead of raising, so without this guard the scan below would pass
    # vacuously (zero files checked) if the core/ tree ever moved.
    assert md_files, (
        f"Expected at least one .md file under {_CORE_DIR}; "
        "directory is missing or the path is wrong, so the scan would "
        "pass without validating anything."
    )
    offenders: list[str] = []
    for path in md_files:
        if _find_scope_boundary_contradictions(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(_CORE_DIR.parent.parent)))
    assert not offenders, (
        "Core proposals promise the throttle actively consumes plan-headroom in "
        "their End state while their Out of scope section defers that wiring to "
        "a follow-up (self-contradictory): " + ", ".join(offenders) + ". "
        "Reword the End state to read-only framing ('surfaces plan-headroom as a "
        "read-only signal the throttle can consult')."
    )


def test_detector_fires_on_seeded_contradiction() -> None:
    """Negative control: an End-state that promises active consumption plus an
    Out-of-scope that defers the wiring must trip the detector."""
    text = (
        "## Description\n\n"
        "End state: the probe threads plan-headroom into the throttle's "
        "awareness and logs a divergence WARNING.\n\n"
        "## Out of scope\n\n"
        "- Wiring plan rate-limit headroom into the throttle's actual gating "
        "math is deferred to proposal 049.\n"
    )
    hits = _find_scope_boundary_contradictions(text)
    assert hits == [
        "the probe threads plan-headroom into the throttle's awareness and "
        "logs a divergence WARNING."
    ]


def test_detector_ignores_read_only_end_state() -> None:
    """Ignore control: read-only End-state phrasing stays silent even alongside
    the same Out-of-scope deferral."""
    text = (
        "## Description\n\n"
        "End state: the probe surfaces plan-headroom as a read-only signal the "
        "throttle can consult (consuming it to gate the loop is deferred to "
        "proposal 049).\n\n"
        "## Out of scope\n\n"
        "- Wiring plan rate-limit headroom into the throttle's actual gating "
        "math is deferred to proposal 049.\n"
    )
    assert _find_scope_boundary_contradictions(text) == []


def test_detector_ignores_contradiction_without_deferral() -> None:
    """Both halves are required: an active-consumption End state without the
    Out-of-scope deferral is not this contradiction."""
    text = (
        "## Description\n\n"
        "End state: the probe threads plan-headroom into the throttle's "
        "awareness.\n\n"
        "## Out of scope\n\n"
        "- Production rollout of the provider.\n"
    )
    assert _find_scope_boundary_contradictions(text) == []


def test_detector_ignores_missing_end_state() -> None:
    """A proposal with the deferral but no End-state narrative never fires."""
    text = (
        "## Out of scope\n\n"
        "- Wiring plan rate-limit headroom into the throttle's actual gating "
        "math is deferred to proposal 049.\n"
    )
    assert _find_scope_boundary_contradictions(text) == []
