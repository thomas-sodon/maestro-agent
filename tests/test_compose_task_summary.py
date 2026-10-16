"""Unit tests for ``maestro.loop.task_record.compose_task_summary``.

The stage is the verbatim lift of ``run``'s inline summary-building block
(proposal 106): it joins the step outputs, appends the kept-branch suffix,
reserves room for a remediation note, and delegates the 500-char budget to the
shared static ``_cap_task_summary``. These tests call the method directly with
keyword args (never through ``agent.run``) and pin each arm; the stage
does no I/O, so the ``make_agent`` fixture (``mem_fs``-backed) needs no cleanup.

Behaviour-preservation of the whole block is proven by the full-``run``
regression harness (``tests/test_agent.py``'s ``test_run_summary_*`` suite,
which now flows through this stage unchanged); these unit tests exercise the
extracted stage's budget arithmetic in isolation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.loop.task_record import compose_task_summary


def test_empty_output_falls_back_to_no_output(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) Empty ``all_output`` → the ``_cap_task_summary`` ``"No output"``
    fallback while the budget can hold it (no branch, no note → available 500)."""
    agent = make_agent()

    summary = compose_task_summary(
        agent,
        all_output=[], kept_branch=None, remediation_note=None,
    )

    assert summary == "No output"


def test_whitespace_only_output_falls_back_to_no_output(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) Whitespace-only ``all_output`` strips to empty, so the same
    ``"No output"`` fallback fires — ``joined`` is falsy after ``.strip()``."""
    agent = make_agent()

    summary = compose_task_summary(
        agent,
        all_output=["   ", "\n\t"], kept_branch=None, remediation_note=None,
    )

    assert summary == "No output"


def test_empty_output_drops_no_output_when_budget_exhausted(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) Empty ``all_output`` with a branch suffix long enough to leave
    ``available < 9`` drops the 9-char ``"No output"`` literal, so the returned
    summary is exactly the branch suffix and stays within the 500-char cap."""
    agent = make_agent()
    long_branch = "b" * 490  # branch_suffix len == len("\n\nBranch: ") + 490 == 500

    summary = compose_task_summary(
        agent,
        all_output=[], kept_branch=long_branch, remediation_note=None,
    )

    assert summary == f"\n\nBranch: {long_branch}"
    assert len(summary) == 500


def test_branch_suffix_present_when_kept(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) A ``kept_branch`` appends the trailing ``\\n\\nBranch: <name>``
    suffix after the joined output."""
    agent = make_agent()

    summary = compose_task_summary(
        agent,
        all_output=["hello"], kept_branch="maestro/task/tid", remediation_note=None,
    )

    assert summary == "hello\n\nBranch: maestro/task/tid"


def test_branch_suffix_absent_when_not_kept(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) ``kept_branch=None`` emits no branch suffix — just the joined output."""
    agent = make_agent()

    summary = compose_task_summary(
        agent,
        all_output=["hello"], kept_branch=None, remediation_note=None,
    )

    assert summary == "hello"
    assert "\n\nBranch:" not in summary


def test_remediation_note_shrinks_budget_but_branch_suffix_survives(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) A non-empty ``remediation_note`` shrinks the available budget by
    ``len(f"{remediation_note} ")`` so the trailing branch suffix survives the
    500-char cap: the body is truncated by exactly that reserve versus the
    no-note case, and the branch suffix is still fully present."""
    agent = make_agent()
    long_output = "x" * 600  # far over budget, so the body is truncated
    branch_suffix = "\n\nBranch: kb"
    note = "note123"
    note_reserve = len(f"{note} ")  # == 8

    with_note = compose_task_summary(
        agent,
        all_output=[long_output], kept_branch="kb", remediation_note=note,
    )
    without_note = compose_task_summary(
        agent,
        all_output=[long_output], kept_branch="kb", remediation_note=None,
    )

    assert with_note.endswith(branch_suffix)
    assert without_note.endswith(branch_suffix)
    # The no-note case fills the whole 500-char budget; the note reserves room,
    # so the body (and thus the total) is shorter by exactly ``note_reserve``.
    assert len(without_note) == 500
    assert len(with_note) == 500 - note_reserve


def test_returns_cap_task_summary_result_with_computed_args(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) The method returns exactly ``_cap_task_summary(joined, branch_suffix,
    note_reserve)`` — spy on the shared helper to pin the delegated positional
    args (stripped join, branch suffix, ``len(f"{note} ")`` reserve) and that its
    return value is passed through unchanged."""
    agent = make_agent()
    sentinel = "CAPPED_SENTINEL"
    agent._cap_task_summary = MagicMock(return_value=sentinel)  # type: ignore[method-assign]

    summary = compose_task_summary(
        agent,
        all_output=["a", "b"], kept_branch="br", remediation_note="rn",
    )

    assert summary == sentinel
    agent._cap_task_summary.assert_called_once_with("a\nb", "\n\nBranch: br", 3)
