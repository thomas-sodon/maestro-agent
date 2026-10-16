"""Derive the owed act once, for the two give-up decisions that bind to it.

Proposal 137, step 4. :func:`~maestro.pr_owed_act.classify_pr_owed_act` is the
single owner of "what does this PR owe me right now?", and the tend walk already
observes that verdict every lap. Two *disposal* decisions still ran on weaker
facts: audit rule 21 (:func:`~maestro.audit_checks_phantom.check_reconcile_near_miss`),
whose finding offered ``gh pr close`` as co-equal to a reconcile the loop owns,
and the salvage oracle (:func:`~maestro.pr_facts.assess_pr_salvage`), which read
only the aggregate ready-and-waiting flag.

This collaborator is where the derivation lives, so ``agent.py`` — then pinned
shrink-only by ``tests/test_source_file_size.py`` — gained two short calls and an
import rather than a body. Both helpers are pure with respect to the host: the
act is read off a :class:`~maestro.pr_facts.PRReadiness` obtained through the
per-iteration-memoized ``observe_pr_readiness`` chokepoint, so the binding
costs no SDK call and at most one readiness observation per PR it is asked
about — free when an earlier phase this iteration already observed that PR
(the salvage caller always hands one in; the audit-phase seam may be the
first reader of a cited PR and then pays for that one observation).

Step 11 adds the third bound decision and its rendering support:
:func:`gather_task_focus` is the whole body of the agent's WIP-primacy gatherer
(so the focus consult reads the owed act without growing ``agent.py``), and
:func:`failing_checks_for` resolves the failing check names the code-red
directive quotes — lazily, at the one site that renders them.

No helper here *mints* an :class:`~maestro.pr_owed_act.OwedAct`: the value is
whatever :func:`~maestro.pr_owed_act.owed_act_from_readiness_snapshot` returns,
so the owed-act structural guard stays clean.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from maestro.audit_checks import log_gate_activation
from maestro.ci_diagnosis import CheckRun, failing_check_names
from maestro.pr_facts import PRReadiness
from maestro.pr_owed_act import OwedAct, owed_act_from_readiness_snapshot
from maestro.remediation_tracker import (
    DISPOSAL_OFFER_GATE,
    format_disposal_offer_detail,
)
from maestro.vcs import VCSError
from maestro.wip_primacy import OpenOwnedPR, TaskFocus, select_task_focus

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from maestro.audit_checks import GateActivationBackend
    from maestro.config import Config
    from maestro.vcs import PrMergeState


def owed_act_for_readiness(readiness: PRReadiness) -> OwedAct | None:
    """The owed act a *readiness* observation implies, or ``None``.

    Thin adapter over
    :func:`~maestro.pr_owed_act.owed_act_from_readiness_snapshot`, which reads
    the ``dataclasses.asdict`` form of the fact defensively: a settled PR
    short-circuits to ``None`` (it is owed nothing), and every blocker is read
    from the already-persisted fields rather than re-fetched. Pure, zero cost.
    """
    snapshot: Mapping[str, object] = dataclasses.asdict(readiness)
    return owed_act_from_readiness_snapshot(snapshot)


def make_owed_act_seam(
    pr_branches: Mapping[int, str],
    observe: Callable[[int, str | None], PRReadiness],
) -> Callable[[int], OwedAct | None]:
    """Build the per-PR ``owed_act`` seam audit rule 21 injects.

    *pr_branches* is the ``{pr_number: branch}`` map the rule-21 block already
    builds from ``get_open_evolution_pr_states``; *observe* is the agent's
    memoized ``observe_pr_readiness`` chokepoint. A PR absent from the map has
    no head to observe — an un-mappable citation, exactly the case the block's
    ``commits_behind`` seam already answers ``None`` for — so the seam returns
    ``None`` and the rule reproduces its unbound statement. Rule 21 consults the
    seam only on the path that emits (and dedupes every later citation of that
    PR), so this makes at most one readiness read per cited PR — served from the
    memo when this iteration already observed it.
    """
    def _owed_act(pr_num: int) -> OwedAct | None:
        branch = pr_branches.get(pr_num)
        if branch is None:
            return None
        return owed_act_for_readiness(observe(pr_num, branch))

    return _owed_act


def make_disposal_offer_recorder(
    pr_branches: Mapping[int, str],
    observe: Callable[[int, str | None], PRReadiness],
    backend: GateActivationBackend,
) -> Callable[[int], None]:
    """Build the ``on_disposal_offer`` callback audit rule 21 takes.

    Each call stamps one ``owed_act_disposal_offer`` gate-activation row for the
    PR rule 21 just offered to close, classified ``diverged`` when the PR reads
    ``CODE_OWED`` through :func:`make_owed_act_seam` and ``aligned`` otherwise
    (an unmapped PR records ``-``). Built regardless of ``[loop]
    owed_act_binding_enabled`` — the reading must exist before that flag is
    flipped — at the cost of one memoized readiness read per offering PR, which
    rule 21's per-rule cap bounds. Measurement only: a host failure on that read
    (:class:`~maestro.vcs.VCSError`) records ``-`` rather than aborting the
    audit, which paid no host read before this sensor existed.
    """
    seam = make_owed_act_seam(pr_branches, observe)

    def _record(pr_num: int) -> None:
        try:
            act = seam(pr_num)
        except VCSError as exc:
            logger.debug(
                "disposal-offer recorder: readiness read for #%d failed: %s",
                pr_num, exc,
            )
            act = None
        log_gate_activation(
            backend,
            gate=DISPOSAL_OFFER_GATE,
            action="diverged" if act is OwedAct.CODE_OWED else "aligned",
            detail=format_disposal_offer_detail(
                pr_num, act.value if act is not None else "",
            ),
        )

    return _record


def readiness_is_code_owed(readiness: PRReadiness) -> bool:
    """Does *readiness* read as ``CODE_OWED`` — a blocker code clears?

    The boolean form :func:`~maestro.pr_facts.assess_pr_salvage` takes. The
    oracle's ``code_owed`` keyword is a plain boolean rather than an
    ``OwedAct`` because ``pr_facts`` is a leaf that ``pr_owed_act`` transitively
    imports, so the comparison against the enum member happens here. Pure.
    """
    return owed_act_for_readiness(readiness) is OwedAct.CODE_OWED


@dataclass(frozen=True)
class OwedActBinding:
    """The flag-aware façade ``agent.py`` consults for both bound decisions.

    One object rather than two flag checks at the call sites, so ``agent.py``
    carries two short
    calls and no branching. *enabled* is ``[loop] owed_act_binding_enabled``;
    when it is ``False`` both methods return the value that reproduces today's
    behaviour exactly (``None`` for each), so the flag-off path costs not even a
    readiness read.
    """

    #: Whether the binding is active (``[loop] owed_act_binding_enabled``).
    enabled: bool

    def code_owed(self, readiness: PRReadiness) -> bool | None:
        """The ``code_owed`` argument :func:`assess_pr_salvage` takes.

        ``None`` while the binding is off — the oracle only holds its veto on a
        literal ``True``, so ``None`` reproduces today's verdict table.
        """
        if not self.enabled:
            return None
        return readiness_is_code_owed(readiness)

    def focus_map(
        self, readiness_by_pr: Mapping[int, PRReadiness],
    ) -> Mapping[int, OwedAct | None]:
        """The ``owed_act_by_pr`` map ``select_task_focus`` takes (137, step 11).

        ``{}`` while the binding is off — an empty map reproduces today's focus
        verdict for every input, and the derivation is never run. When on, every
        already-observed readiness in *readiness_by_pr* is turned into its owed
        act through :func:`owed_act_for_readiness`, so the map costs no
        additional observation beyond the ones the caller already made.
        """
        if not self.enabled:
            return {}
        return {
            pr_num: owed_act_for_readiness(readiness)
            for pr_num, readiness in readiness_by_pr.items()
        }

    def seam(
        self,
        pr_branches: Mapping[int, str],
        observe: Callable[[int, str | None], PRReadiness],
    ) -> Callable[[int], OwedAct | None] | None:
        """The ``owed_act`` seam audit rule 21 takes, or ``None`` when off.

        A ``None`` seam makes rule 21 reproduce its unbound finding text
        byte-for-byte, and — because the seam is never built — observes no
        readiness at all.
        """
        if not self.enabled:
            return None
        return make_owed_act_seam(pr_branches, observe)


def owed_act_binding(config: Config) -> OwedActBinding:
    """Read ``[loop] owed_act_binding_enabled`` into an :class:`OwedActBinding`."""
    return OwedActBinding(enabled=config.owed_act_binding_enabled)


def gather_task_focus(
    open_pr_states: Mapping[str, PrMergeState],
    observe: Callable[[int, str | None], PRReadiness],
    binding: OwedActBinding,
) -> TaskFocus:
    """Build :func:`~maestro.wip_primacy.select_task_focus`'s inputs and ask it.

    The pure body of the agent's WIP-primacy gatherer (proposal 137, step 11),
    kept here rather than in the thin ``agent.py``. *open_pr_states* is
    the ``{head_ref: PrMergeState}`` enumeration
    :meth:`~maestro.vcs.CodeHost.get_open_evolution_pr_states` returns (already
    snapshotted per iteration by the caller); *observe* is the agent's
    per-iteration-memoized ``observe_pr_readiness`` chokepoint. Each state
    contributes its identity/age and one readiness observation, the *binding*
    derives the owed-act map from those same observations, and
    ``select_task_focus`` decides. Zero SDK cost, no host call of its own.
    """
    open_prs: list[OpenOwnedPR] = []
    readiness_by_pr: dict[int, PRReadiness] = {}
    for branch, state in open_pr_states.items():
        open_prs.append(
            OpenOwnedPR(pr_number=state.number, created_at=state.created_at),
        )
        readiness_by_pr[state.number] = observe(state.number, branch)
    return select_task_focus(
        open_prs,
        readiness_by_pr,
        owed_act_by_pr=binding.focus_map(readiness_by_pr),
    )


def failing_checks_for(
    pr_number: int,
    observe_check_runs: Callable[[int], Sequence[CheckRun]],
) -> tuple[str, ...]:
    """The names of *pr_number*'s concretely-failing checks, or ``()``.

    Runs the injected check-run reader (the agent's ``_observe_pr_check_runs``,
    which maps :meth:`~maestro.vcs.CodeHost.get_pr_failing_check_names` into
    :class:`~maestro.ci_diagnosis.CheckRun`s) through the single CI-truth owner
    :func:`~maestro.ci_diagnosis.failing_check_names`, so the names are deduped
    and order-preserving. An empty read — a name-less aggregate rollup, or a
    host that reported nothing — yields ``()`` and the caller keeps its
    name-free text. Resolved lazily at the one site that renders the names, so
    nothing is fetched unless that site actually fires. Pure with respect to the
    host beyond the one injected read.
    """
    return failing_check_names(tuple(observe_check_runs(pr_number)))
