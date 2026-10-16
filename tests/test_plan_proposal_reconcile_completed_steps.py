"""Regression: plan-proposal.md reconciles completed steps into the proposal.

`/plan-proposal` used to be strictly read-only against `docs/proposals/`: it
verified the proposal against today's tree only to ground the plan file it
wrote under `~/.claude/plans/`, and left the proposal document stale even when
its steps had already landed in a prior merge. The command now folds
`/update-proposals`' *status axis* into planning — before writing the plan it
flips completed-step checkboxes `[ ]` → `[x]`, re-derives the metadata `Status`
and the `What landed` / `What is missing` rows, and reconciles the single
proposal's README row — so the plan's "next increment" is derived from an
accurate view of what remains. The reconciliation is deliberately bounded to
the status axis; broader drift (paths, line-number citations, symbol renames,
dependency prose) stays `/update-proposals`' job.

This test locks in the new section and the matching carve-outs in *What to
skip* and *Safety*, which previously declared the command read-only against the
proposal files.

Static-text only: reads the command doc, no `mem_fs`/subprocess/network.
"""

from __future__ import annotations

from pathlib import Path

_PLAN_PROPOSAL_DOC = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "commands"
    / "plan-proposal.md"
)

_RECONCILE_HEADER = "## Reconcile completed steps into the proposal"


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def _reconcile_section(doc: str) -> str:
    return _section_between(
        doc, _RECONCILE_HEADER, "## Research what the proposal does not say"
    )


def test_reconcile_section_present() -> None:
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")
    assert _RECONCILE_HEADER in doc, (
        "plan-proposal.md must carry a `## Reconcile completed steps into the "
        "proposal` section so planning writes landed-step completions back into "
        "the proposal, not just into the plan file."
    )


def test_reconcile_flips_completed_step_checkboxes() -> None:
    """The section must flip a landed step's checkbox `[ ]` → `[x]`, marker only."""
    section = _reconcile_section(_PLAN_PROPOSAL_DOC.read_text(encoding="utf-8"))

    assert "[ ]" in section and "[x]" in section and "flip" in section.lower(), (
        "The reconcile section must flip a landed step's marker `N. [ ]` → "
        f"`N. [x]`. Section:\n{section}"
    )
    assert "verbatim" in section.lower(), (
        "The reconcile section must flip the marker only, leaving the step's "
        f"prose verbatim. Section:\n{section}"
    )


def test_reconcile_bounded_to_status_axis_defers_broader_drift() -> None:
    """The section must stay on the status axis and defer the rest to update-proposals."""
    section = _reconcile_section(_PLAN_PROPOSAL_DOC.read_text(encoding="utf-8"))

    assert "status axis" in section.lower(), (
        "The reconcile section must bound itself to the status axis (checkboxes "
        f"and status), not full drift. Section:\n{section}"
    )
    assert "/update-proposals" in section, (
        "The reconcile section must defer broader drift axes (paths, citations, "
        f"symbols, dependencies) to `/update-proposals`. Section:\n{section}"
    )


def test_reconcile_skips_docs_only_proposals() -> None:
    """A docs-only / service proposal with no checkbox step list is skipped."""
    section = _reconcile_section(_PLAN_PROPOSAL_DOC.read_text(encoding="utf-8"))

    assert "docs-only" in section.lower(), (
        "The reconcile section must skip a docs-only / service-flavored proposal "
        f"that has no checkbox step list to flip. Section:\n{section}"
    )


def test_what_to_skip_carves_out_the_reconciliation() -> None:
    """*What to skip* must no longer declare the proposal never written back."""
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")
    skip = _section_between(doc, "## What to skip", "## Safety")

    assert "never written back to `docs/proposals/`" not in skip, (
        "The stale absolute — the proposal is `never written back to "
        "`docs/proposals/`` — must be gone now that planning reconciles the "
        f"status axis. Section:\n{skip}"
    )
    assert "status-axis reconciliation" in skip, (
        "*What to skip* must carve out the bounded status-axis reconciliation as "
        f"the one sanctioned write to a proposal file. Section:\n{skip}"
    )


def test_safety_admits_proposal_and_readme_write() -> None:
    """*Safety* must admit the bounded proposal/README write, not claim read-only."""
    doc = _PLAN_PROPOSAL_DOC.read_text(encoding="utf-8")
    safety = doc[doc.index("## Safety") :]

    assert "status axis" in safety.lower(), (
        "The Safety note must admit the bounded status-axis write to the "
        f"proposal file and its README row. Safety:\n{safety}"
    )
    assert _RECONCILE_HEADER.removeprefix("## ") in safety, (
        "The Safety note must point at *Reconcile completed steps into the "
        f"proposal* as the source of the sanctioned write. Safety:\n{safety}"
    )
