"""Regression tests for #4222 — rule 20 must not credit a confirmed landing.

:func:`maestro.audit_checks_phantom.check_undercredited_landing` shares rule 14's
CONFIRMED-attribution skip: a summary confirming *another party's* landing is
not an own landing to credit, so it is dropped before any SHA extraction or
ancestry probe. Split out of ``tests/test_audit_checks.py``, whose size is
pinned shrink-only.
"""

from __future__ import annotations

from maestro.audit_checks_phantom import (
    check_undercredited_landing,
)
from maestro.memory import PlanStep, TaskRecord
from maestro.types import TaskOutcome

# Carries hex letters so it matches SELF_COMMITTED_SHA_RE.
_LANDED_SHA = "ac48a769"

_CONFIRMED_LANDING_SUMMARY = (
    f"Already merged before I touched it; resolved on branch for PR #2638, "
    f"committed {_LANDED_SHA}."
)

_CAUSED_LANDING_SUMMARY = (
    f"Landed the 2-parent merge; committed {_LANDED_SHA} — "
    f"origin/main now a true ancestor."
)


def _task(summary: str, *, outcome: TaskOutcome = "partial") -> TaskRecord:
    """A non-success task whose summary cites a committed SHA."""
    return TaskRecord(
        task_id="t1",
        goal="Land the blocker PR",
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome=outcome,
        result_summary=summary,
        errors=[],
        duration_secs=0.5,
        timestamp="2026-07-15T00:00:00Z",
    )


def _no_probe(_sha: str) -> bool:
    raise AssertionError("ancestry probe must not run for a confirmation")


def test_undercredited_landing_skips_confirmed_attribution() -> None:
    """A confirmation of another's land is skipped before any probe."""
    findings = check_undercredited_landing(
        [_task(_CONFIRMED_LANDING_SUMMARY)],
        enabled=True,
        is_ancestor=_no_probe,
    )
    assert findings == []


def test_undercredited_landing_skips_confirmed_attribution_on_failure() -> None:
    """The CONFIRMED skip holds for a ``failure`` outcome too."""
    findings = check_undercredited_landing(
        [_task(_CONFIRMED_LANDING_SUMMARY, outcome="failure")],
        enabled=True,
        is_ancestor=_no_probe,
    )
    assert findings == []


def test_undercredited_landing_still_fires_on_caused_summary() -> None:
    """The skip is scoped to CONFIRMED only — a CAUSED summary still fires."""
    findings = check_undercredited_landing(
        [_task(_CAUSED_LANDING_SUMMARY)],
        enabled=True,
        is_ancestor=lambda s: True,
    )
    assert len(findings) == 1
    assert findings[0].kind == "undercredited_landing"
