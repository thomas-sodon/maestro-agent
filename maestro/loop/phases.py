"""Phase-runner registry seam carved out of ``maestro.agent`` (proposal 106).

The loop's ``run_loop`` dispatch is a single ``for phase`` walk over
:data:`maestro.agent._LOOP_PHASES` whose body once inlined a per-phase
``if/elif phase == "..."`` ladder. This module holds the *runner-order* data
that let the dispatch replace those branches with a single table lookup,
mirroring the way ``lifecycle.py`` holds the ``_BUDGET_GATED_PHASES``
membership data the *gate* half consults and ``pr_remediation.py`` holds the
``_DEQUEUE_GATE_TABLE`` ordered registry the dequeue face walks.

:data:`REGISTERED_PHASE_NAMES` now covers *every* ``_LOOP_PHASES`` member: each
one's runner is a named ``MaestroAgent._phase_*`` method, so the inline ladder
is gone and the dispatch is one ``self._phase_runners[phase](...)`` call. The
persistent-session phases (``contemplate`` / ``evolve`` / ``curate_pending_plans`` share
the promoted ``self._session_a`` / ``self._session_b`` instance attributes) are
registered alongside the mechanical phases; an unregistered phase now fails
loud with a ``KeyError`` instead of silently falling through.

No behaviour lives here — only the order data and a validator the agent calls
once at construction so a drift between this sequence and ``_LOOP_PHASES``
fails loud.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

#: A migrated phase runner. Every registered runner shares one signature —
#: ``async def _phase_<name>(self, *, iteration: int) -> None`` bound to the
#: agent — so the dispatch can invoke any of them uniformly with the current
#: iteration index. ``iteration`` is accepted by every runner even though only
#: ``_phase_tend_evolution_prs`` (its ``_reconcile_settled_tend_prs`` tail),
#: ``_phase_check_merged_plans`` (its ``_reconcile_settled_merge_check`` tail),
#: ``_phase_audit_self_model`` (forwarded to ``_audit_self_model``), and
#: ``_phase_contemplate`` (forwarded to ``contemplate``) use it.
PhaseRunner = Callable[..., Awaitable[None]]

#: The full ``_LOOP_PHASES`` sequence, every member migrated onto the runner
#: registry. Order is load-bearing: it must stay an order-preserving (here,
#: identity) subsequence of ``_LOOP_PHASES`` so the table lookup can never
#: reorder phases. :func:`validate_registered_phase_order` pins this at
#: construction and ``tests/test_loop_phases.py`` pins it in CI.
REGISTERED_PHASE_NAMES: tuple[str, ...] = (
    "ensure_main_branch",
    "check_contributions",
    "process_messages",
    "tend_evolution_prs",
    "attend",
    "contemplate",
    "respond_to_messages",
    "implement_approved_plans",
    "check_merged_plans",
    "review_artifacts",
    "execute_approved_publications",
    "process_rejected_plans",
    "generate_task",
    "run_task",
    "evolve",
    "audit_self_model",
    "curate_pending_plans",
    "dream",
)


def validate_registered_phase_order(loop_phases: tuple[str, ...]) -> None:
    """Assert :data:`REGISTERED_PHASE_NAMES` covers ``loop_phases`` in order.

    Every registered name must appear in ``loop_phases`` and in the same
    relative order, so routing a phase through the runner table can never move
    it ahead of or behind another phase in the walk. Because the registry now
    covers *every* phase (the inline ``elif`` ladder is gone and the dispatch
    is a hard ``self._phase_runners[phase]`` lookup with no fall-through), the
    reverse cover must hold too: every ``loop_phases`` member must be
    registered, or that phase would surface its absence only as a mid-loop
    :class:`KeyError` at dispatch instead of failing loud here at construction.
    Raises :class:`ValueError` on the first drift (a registered name missing
    from ``loop_phases``, two registered names whose order disagrees with it,
    or a ``loop_phases`` member with no registered runner).
    """
    positions: list[int] = []
    for name in REGISTERED_PHASE_NAMES:
        try:
            positions.append(loop_phases.index(name))
        except ValueError:
            raise ValueError(
                f"Registered phase {name!r} is not a member of _LOOP_PHASES",
            ) from None
    if positions != sorted(positions):
        raise ValueError(
            "REGISTERED_PHASE_NAMES is not an order-preserving subsequence of "
            f"_LOOP_PHASES: {REGISTERED_PHASE_NAMES!r} vs {loop_phases!r}",
        )
    registered = set(REGISTERED_PHASE_NAMES)
    unregistered = [name for name in loop_phases if name not in registered]
    if unregistered:
        raise ValueError(
            "Every _LOOP_PHASES member must have a registered runner or the "
            "hard dispatch lookup would fail with a mid-loop KeyError; "
            f"unregistered phases: {unregistered!r}",
        )
