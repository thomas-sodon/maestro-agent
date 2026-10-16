"""Actuate a directive that names the ``mark_plan_obsolete`` lever.

Contemplation often writes a ``next_task_directive`` asking for one zero-cost
act — "obsolete pending plan <id> via mark_plan_obsolete". Its only consumer,
``generate_task``, does not run while ``[loop] self_directed_tasks_enabled`` is
off, and even when it runs the directive only steers an LLM-curated task. The
directive therefore sat in the slot while the priced phases re-narrated it.

:func:`actuate_obsolete_directive` fires that lever mechanically from the
zero-SDK ``audit_self_model`` phase, behind ``[loop]
obsolete_directive_actuator_enabled``. The aim and the fire are
:func:`~maestro.lever_capture.fire_directive_named_obsolete`'s; this
collaborator records the deed, the event and the gate activation, then clears
the directive it acted on. It lives here rather than in ``agent.py``, which
``tests/test_source_file_size.py`` pins shrink-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from maestro import deed_ledger
from maestro.audit_checks import log_gate_activation
from maestro.lever_capture import DirectiveObsoleteKind, fire_directive_named_obsolete

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.consciousness import SelfModel


def actuate_obsolete_directive(
    agent: MaestroAgent, self_model: SelfModel,
) -> SelfModel:
    """Fire the obsolete lever a directive names; return the current self-model.

    A ``FIRED`` outcome records one ``OWNED_PRUNE`` deed, one journal event and
    one ``obsolete_directive_actuator`` gate activation. ``FIRED``,
    ``STALE`` and ``UNFIREABLE`` clear the slot through ``mutate_self_model`` — only while the
    live directive still equals the evaluated string, so a concurrent rewrite
    survives — and return the reloaded model. ``NOOP`` (or an empty slot)
    returns *self_model* unchanged. Exceptions propagate (fail loud).
    """
    directive = self_model.next_task_directive
    if not directive:
        return self_model
    outcome = fire_directive_named_obsolete(
        agent.memory, agent.lever_attempts, directive,
    )
    if outcome.kind == DirectiveObsoleteKind.NOOP:
        return self_model
    plan_id = outcome.plan_id or ""
    if outcome.kind == DirectiveObsoleteKind.FIRED:
        agent.deed_ledger.record(
            deed_ledger.DeedType.OWNED_PRUNE,
            None,
            detail=f"directive-named obsolete pruned pending plan {plan_id}",
        )
        agent.journal.record_event(
            "Obsolete-directive actuator: my next_task_directive named "
            f"pending plan {plan_id} for mark_plan_obsolete, so I retired it "
            "mechanically at zero SDK cost and cleared the directive.",
            entry_type="reflection",
            triggered_by="obsolete_directive_actuator",
        )
        log_gate_activation(
            agent.gate_activations,
            gate="obsolete_directive_actuator",
            action="fired",
            detail=plan_id,
        )

    def _clear(live: SelfModel) -> None:
        if live.next_task_directive == directive:
            live.next_task_directive = None

    agent.consciousness.mutate_self_model(_clear)
    return agent.consciousness.load_self_model()
