"""Tests for the extracted ``maestro.loop.task_record.apply_merge_clean_promotion_directive``.

This is ``run``'s ``# 5g.`` block lifted into a named, independently-testable
stage: when a kept branch merges cleanly and no directive is already queued, it
steers the next ``generate_task`` to promote it via ``source_task_id`` before it
rots. The extraction is behaviour-preserving, so these tests pin the exact
promotion string, the empty-slot deference, the master-switch gate, and the
``merge_clean is True`` (not merely truthy) guard.
"""

from __future__ import annotations

import pytest

from maestro.loop.task_record import apply_merge_clean_promotion_directive


def test_directive_set_on_clean_empty_slot(make_agent):  # type: ignore[no-untyped-def]
    """merge_clean True + flag on + empty slot → exact promotion string queued."""
    agent = make_agent()
    # Precondition: no directive queued.
    assert not agent.consciousness.load_self_model().next_task_directive

    apply_merge_clean_promotion_directive(
        agent,
        merge_clean=True,
        kept_branch="maestro/task/abc123",
        task_id="abc123",
    )

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == (
        "Branch maestro/task/abc123 merges cleanly — "
        "promote via source_task_id=abc123"
    )


def test_directive_preserved_when_already_queued(make_agent):  # type: ignore[no-untyped-def]
    """An already-populated directive is never overwritten (empty-slot only)."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "existing directive"
    agent.consciousness.save_self_model(model)

    apply_merge_clean_promotion_directive(
        agent,
        merge_clean=True,
        kept_branch="maestro/task/xyz",
        task_id="xyz",
    )

    updated = agent.consciousness.load_self_model()
    assert updated.next_task_directive == "existing directive"


def test_no_write_when_promotion_pressure_disabled(make_agent):  # type: ignore[no-untyped-def]
    """Master switch off → no promotion directive, even with an empty slot."""
    agent = make_agent()
    agent.config.branch_promotion_pressure_enabled = False
    assert not agent.consciousness.load_self_model().next_task_directive

    apply_merge_clean_promotion_directive(
        agent,
        merge_clean=True,
        kept_branch="maestro/task/off",
        task_id="off",
    )

    assert not agent.consciousness.load_self_model().next_task_directive


@pytest.mark.parametrize("verdict", [False, None])
def test_no_write_when_not_clean(make_agent, verdict):  # type: ignore[no-untyped-def]
    """A non-True verdict (conflicting ``False`` / unknown ``None``) writes nothing.

    Only an exact ``merge_clean is True`` may queue the hint; a regression such
    as ``if merge_clean is not None`` would fabricate a bogus directive here.
    """
    agent = make_agent()
    assert not agent.consciousness.load_self_model().next_task_directive

    apply_merge_clean_promotion_directive(
        agent,
        merge_clean=verdict,
        kept_branch="maestro/task/nc",
        task_id="nc",
    )

    assert not agent.consciousness.load_self_model().next_task_directive
