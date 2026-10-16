"""Static-text guards for ``.claude/commands/graduate-proposal.md``.

Same family as ``tests/test_plan_proposal_command_doc.py``: the command doc
carries a load-bearing contract — the full graduation mechanism set, the
burn-in readiness gate that routes non-graduatable proposals to
``/plan-proposal``, the merged-branch switch that fast-forwards local ``main``,
and the human-in-the-loop boundary that keeps the command from editing config,
the flag manifests, the proposal docs, or ``CLAUDE.md`` (those edits are
``/impl``'s job). A silent drift in any of those would make the command mint a
graduation plan that misses a mechanism (an un-incremented ``_MAX_CUTOVER_FLAGS``
or a stale census count reds CI) or step outside its read-mostly contract.

Static-text only: reads the command doc, no ``mem_fs``/subprocess/network/sleep.
"""

from __future__ import annotations

from pathlib import Path

from tests.support.command_docs import assert_no_restated_substate_criterion

_GRADUATE_PROPOSAL_DOC = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "commands"
    / "graduate-proposal.md"
)


def _doc() -> str:
    return _GRADUATE_PROPOSAL_DOC.read_text(encoding="utf-8")


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_merged_branch_switch_fast_forwards_local_main() -> None:
    """The merged-branch switch must fast-forward local ``main``.

    ``git fetch origin main`` advances only the ``origin/main`` remote-tracking
    ref; a plain ``git switch main`` then lands on a stale local ``main`` the
    fetch never moved, grounding the graduation plan in old code. The exit-0
    path must fast-forward local ``main`` to ``origin/main``.
    """
    doc = _doc()

    section = _section_between(
        doc,
        "## Switch to main if on a merged branch",
        "## Resolve the proposal and gate on graduation-readiness",
    )
    assert "git merge --ff-only origin/main" in section, (
        "The merged-branch switch must fast-forward local `main` to "
        "`origin/main` after `git switch main`, or grounding reads a stale "
        f"local `main` the fetch never advanced. Section:\n{section}"
    )


def test_argument_normalization_strips_leading_zeros_and_rejects_overlong() -> None:
    """The proposal-number normalization must be deterministic.

    An already-padded input like ``040`` is ambiguous unless leading zeros are
    stripped before re-padding (octal ``040`` → 32 vs decimal → 40); and a
    4-digit input like ``1000`` cannot be zero-padded to 3 digits at all, so it
    must be rejected up front with ``error=invalid_argument`` rather than
    drifting into an accidental empty-glob ``error=proposal_not_found``.
    """
    doc = _doc()

    section = _section_between(
        doc, "## Argument parse", "## Switch to main if on a merged branch"
    )
    lowered = section.lower()
    assert "strip" in lowered and "leading zero" in lowered, (
        "The normalization rule must strip leading zeros before re-padding so "
        f"an already-padded input like `040` normalizes deterministically. "
        f"Section:\n{section}"
    )
    assert "more than 3 digits" in section, (
        "The normalization rule must name the overlong-input case (more than 3 "
        f"digits, e.g. `1000`). Section:\n{section}"
    )
    overlong_idx = section.index("more than 3 digits")
    line_start = section.rfind("\n", 0, overlong_idx) + 1
    line_end = section.index("\n", overlong_idx)
    overlong_line = section[line_start:line_end]
    assert "error=invalid_argument" in overlong_line, (
        "The overlong-input case must reject with `error=invalid_argument` on "
        f"the same normalization line. Line:\n{overlong_line}"
    )


def test_readiness_gate_routes_non_burn_in_to_plan_proposal() -> None:
    """The readiness gate must recommend ``/plan-proposal`` and never invoke it.

    The command graduates a burn-in proposal; a proposal whose behaviour-code
    is not built yet (``proposed`` / ``partial`` / ``in-progress``) is
    ``/plan-proposal``'s job, not this one's. The gate must route there by
    *recommendation* — this command is the mirror-complement and must never
    invoke the sibling.
    """
    doc = _doc()

    section = _section_between(
        doc,
        "## Resolve the proposal and gate on graduation-readiness",
        "## Ground the flags first-hand",
    )
    assert "/plan-proposal" in section, (
        "The readiness gate must route a non-burn-in proposal to "
        f"`/plan-proposal`. Section:\n{section}"
    )
    assert "recommend" in section.lower(), (
        "The readiness gate must *recommend* `/plan-proposal`, not invoke it. "
        f"Section:\n{section}"
    )
    assert "never invoke" in doc.lower(), (
        "The doc must state the never-invoke contract: it recommends the "
        "sibling command and the human runs it, but never invokes it."
    )


def test_grounded_cutover_flag_stops_out_of_scope() -> None:
    """An already-cut-over flag has no in-scope increment — grounding must STOP.

    ``burn-in:graduated`` is admitted at the status-token gate, but a flag
    already grounded as a ``CUTOVER_FLAGS`` entry has cut over: its only
    remaining step is the collapse/decommission this command scopes out. Without
    a stop-gate keyed to the *grounded* stage, the command would mint an empty
    graduation plan for such a proposal. The grounding section must STOP when
    every flag has already cut over.
    """
    doc = _doc()

    section = _section_between(
        doc,
        "## Ground the flags first-hand",
        "## Enumerate the next graduation increment",
    )
    assert "CUTOVER_FLAGS" in section, (
        "The grounding section must key its stop-gate to an already-cut-over "
        f"`CUTOVER_FLAGS` flag. Section:\n{section}"
    )
    lowered = section.lower()
    assert "stop" in lowered, (
        "The grounding section must STOP when every flag has already cut over — "
        f"there is no in-scope cutover increment left to plan. Section:\n{section}"
    )
    assert "collapse" in lowered and "scope" in lowered, (
        "The grounding stop-gate must name the collapse/decommission as the "
        f"out-of-scope later increment. Section:\n{section}"
    )


def test_mechanism_enumeration_names_every_family() -> None:
    """The enumeration must name every graduation mechanism family.

    Graduation is spread across many surfaces; a generated plan that misses one
    reds CI (an un-incremented ``_MAX_CUTOVER_FLAGS``, a stale census docstring,
    a forgotten ``PROD_ONLY_CANARY_BASELINE`` member, a wrong
    ``maestro.production.toml`` reversion count). Pin each family so the doc
    cannot silently drop one.
    """
    doc = _doc()

    section = _section_between(
        doc,
        "## Enumerate the next graduation increment",
        "## Write and record the graduation plan",
    )
    required = (
        "EXPERIMENTAL_FLAGS",
        "CUTOVER_FLAGS",
        "census",
        "_UNMANIFESTED_DEFAULT_OFF",
        "_MAX_CUTOVER_FLAGS",
        "_flag_manifested_default_off",
        "_flag_manifested_cutover_default_on",
        "PROD_ONLY_CANARY_BASELINE",
        "maestro.production.toml",
        "Reversion",
        "[production: …]",
        "| **Status** |",
        "docs/proposals/README.md",
        "CLAUDE.md",
    )
    for token in required:
        assert token in section, (
            f"The graduation-mechanism enumeration must name {token!r} so a "
            f"generated plan cannot silently drop that surface. Section:\n{section}"
        )


def test_human_in_the_loop_write_boundary() -> None:
    """The command must write only a plan, never perform the graduation edits.

    Graduation stays human-in-the-loop: this command plans the edits; ``/impl``
    performs them under review. The Safety section must forbid editing config,
    the flag manifests, the proposal docs, and ``CLAUDE.md``, and forbid
    committing/pushing/opening a PR.
    """
    doc = _doc()

    safety = doc[doc.index("## Safety") :]
    assert "never commits, stages, pushes, opens a PR, or files an issue" in safety, (
        "Safety must forbid committing/staging/pushing/opening a PR/filing an "
        f"issue. Section:\n{safety}"
    )
    assert "never edits" in safety.lower(), (
        "Safety must state the command never edits config/manifests/docs — it "
        f"plans those edits for `/impl`. Section:\n{safety}"
    )
    for token in (
        "maestro/config.py",
        "maestro/flag_lifecycle.py",
        "docs/proposals",
        "CLAUDE.md",
    ):
        assert token in safety, (
            f"Safety must name {token!r} among the surfaces the command never "
            f"edits. Section:\n{safety}"
        )
    assert "writes a self-contained graduation plan" in doc, (
        "The doc must state that the command writes a graduation plan (its sole "
        "deliverable)."
    )


def test_burn_in_criteria_are_not_restated() -> None:
    """This doc must not carry its own copy of the substate entry criteria.

    The criteria that pick between ``burn-in:ready`` / ``burn-in:observing`` /
    ``burn-in:graduated`` live in one place — the ``### Status Key`` of
    ``docs/proposals/README.md``. Naming the three tokens is fine; defining them
    here is a second carrier that drifts. Mirrors
    ``test_plan_proposal_command_doc.py::test_burn_in_criteria_are_not_restated``.
    """
    assert_no_restated_substate_criterion(_doc(), "graduate-proposal.md")
