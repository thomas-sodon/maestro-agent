"""Per-open-PR *owed-act* verdict — compose existing oracles into one label.

The measured waste this addresses is the CI-pending remediation treadmill: a
loop laps an own PR whose only blocker is the CI clock, re-recording ``partial``
every cycle, because no surface answers — *before* I touch a PR — whether its
owed next act is **write code**, **hand it off to Thomas**, or **wait for the
CI clock**. ``pr-lap-ledger`` counts laps and ``deed-ledger`` reports fired
deeds, but neither classifies the owed act; Tend's clearable-blocker set is
internal routing, never surfaced.

This module is that missing classification, kept pure and read-only. It
**composes existing oracles only** — no new merge-oracle call path, no network:

* :func:`maestro.remediation_tracker.classify_blocker_ownership` — is the merge
  blocker *mine* to clear by writing code, or external?
* :func:`maestro.ci_diagnosis.classify_ci_conclusions` — is CI concluded-failing,
  still-running, or all-green?
* a **caller-provided** settled-PR set — a PR already settled (merged / closed)
  is owed nothing, so it short-circuits *before* any oracle is consulted.

:func:`classify_pr_owed_act` folds those three into exactly one :class:`OwedAct`
(or ``None`` for a settled PR). It never fetches merge truth itself — the settled
set is passed in — so it adds zero merge-oracle calls and cannot push, merge, or
reset a CI clock. :func:`check_runs_from_conclusion` expands the single aggregate
rollup string a caller already holds back into the
:class:`~maestro.ci_diagnosis.CheckRun` vocabulary the CI oracle reads, and
:func:`check_runs_from_ci_state` does the same for the ``CIState`` vocabulary a
persisted readiness snapshot carries — the expander
:func:`owed_act_from_readiness_snapshot` reads through to derive the owed act
from an already-persisted ``pr_readiness_snapshot`` with no host call at all.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet

from maestro import ci_diagnosis, pr_facts, remediation_tracker
from maestro.ci_diagnosis import CheckRun


class OwedAct(enum.Enum):
    """The single owed next act an open own PR presents, from my perspective.

    * :attr:`CODE_OWED` — I must write code: an owned merge blocker (conflict /
      unresolved thread / unpushed resolution) or a concluded-failing CI red.
    * :attr:`HANDOFF_READY` — green CI, no owned-code blocker left, and no
      host-reported behind/merge gate; the only gate is human approval, so the
      ball is in Thomas's court — hand it off. **Not authoritative for
      behind-ness:** behind-``main`` is seen only via the caller's
      ``mergeStateStatus`` proxy (the compose-only design plumbs no measured
      ``commits_behind``), so a behind-but-clean PR the proxy masks as ``CLEAN``
      lands here rather than in :attr:`EXTERNAL_BLOCKED`.
    * :attr:`WAIT_CI` — CI is still running: neither I nor Thomas act, the clock
      does. This is the treadmill state a lap must *not* re-remediate.
    * :attr:`EXTERNAL_BLOCKED` — an external, non-handoff gate (a behind-``main``
      branch Tend catches up mechanically — only under
      ``[loop] tend_main_merge_enabled`` — or any other blocker neither my code
      nor a bare approval clears yet).
    """

    CODE_OWED = "code-owed"
    HANDOFF_READY = "handoff-ready"
    WAIT_CI = "wait-ci"
    EXTERNAL_BLOCKED = "external-blocked"


#: One-line human gloss per verdict, for the ``pr-owed-act`` CLI surface.
_OWED_ACT_DESCRIPTIONS: dict[OwedAct, str] = {
    OwedAct.CODE_OWED: "write code — owned blocker or CI red",
    OwedAct.HANDOFF_READY: "hand off to Thomas — green, awaiting approval",
    OwedAct.WAIT_CI: "wait for the CI clock — checks still running",
    OwedAct.EXTERNAL_BLOCKED: "externally blocked — mechanical / non-handoff gate",
}

#: Green-CI external blockers that mean "the ball is in Thomas's court": the only
#: remaining gate is human approval (:data:`~maestro.remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL`)
#: or nothing at all (:data:`~maestro.remediation_tracker.MERGE_BLOCK_NONE`), so a
#: green PR here is handoff-ready rather than externally stuck. Every *other*
#: external blocker — a behind-``main`` branch Tend catches up mechanically, say
#: (a premise that holds only under ``[loop] tend_main_merge_enabled``; the
#: merge-queue model resolves behind-ness away in :func:`derive_owed_act_blocker`) —
#: stays :attr:`OwedAct.EXTERNAL_BLOCKED`.
_HANDOFF_GREEN_BLOCKERS = frozenset(
    {
        remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL,
        remediation_tracker.MERGE_BLOCK_NONE,
    },
)


def describe_owed_act(act: OwedAct) -> str:
    """Return the one-line human gloss for *act* (for the CLI surface)."""
    return _OWED_ACT_DESCRIPTIONS[act]


def classify_pr_owed_act(
    pr_number: int,
    *,
    blocker: str,
    check_runs: Sequence[CheckRun],
    settled_pr_numbers: AbstractSet[int],
    own_push: bool = False,
    unresolved_threads: int = 0,
) -> OwedAct | None:
    """Classify one open own PR's owed next act, composing existing oracles.

    Returns exactly one :class:`OwedAct` for an unsettled PR, or ``None`` when
    *pr_number* is in *settled_pr_numbers* — a settled (merged / closed) PR is
    owed nothing, and that check short-circuits **before any oracle is
    consulted** (verified under test), so a settled PR costs zero classifier
    work and is excluded from the surfaced verdicts.

    The decision tree, once past the settled short-circuit:

    0. When *unresolved_threads* is positive, the *blocker* is first promoted via
       :func:`~maestro.remediation_tracker.classify_owned_blocker`: an approval
       wait or a clean merge state with an open AI review thread is owned,
       code-clearable work, not an external/handoff gate, so it becomes
       :data:`~maestro.remediation_tracker.MERGE_BLOCK_THREADS_UNRESOLVED` before
       ownership is read. A conflict / behind / CI blocker names a different,
       still-blocking gate and is left unchanged. *unresolved_threads* is
       **caller-supplied** — the module fetches no threads itself, preserving its
       no-network contract — and defaults to ``0``, so a caller that passes no
       count gets byte-identical pre-promotion behaviour.
    1. :func:`~maestro.remediation_tracker.classify_blocker_ownership` on the
       (possibly promoted) *blocker* (honouring *own_push*): an *owned* blocker —
       a conflict, an unresolved review thread, or (with *own_push*) an unpushed
       resolution — is code I must write, so it returns :attr:`OwedAct.CODE_OWED`
       regardless of CI.
    2. Otherwise the blocker is external; the CI axis decides via
       :func:`~maestro.ci_diagnosis.classify_ci_conclusions` on *check_runs*:
       ``FAILING`` (a concluded red is my code) → :attr:`OwedAct.CODE_OWED`;
       ``RUNNING`` (checks queued / in progress) → :attr:`OwedAct.WAIT_CI`.
    3. CI is ``GREEN``: an approval-wait or a clean merge state
       (:data:`_HANDOFF_GREEN_BLOCKERS`) is :attr:`OwedAct.HANDOFF_READY` — the
       ball is in Thomas's court — while every other external gate is
       :attr:`OwedAct.EXTERNAL_BLOCKED`. Caveat: *behind-ness enters only through
       the caller's ``blocker``*; a behind-but-clean PR whose host
       ``mergeStateStatus`` proxy does not report ``BEHIND`` yields
       :data:`~maestro.remediation_tracker.MERGE_BLOCK_NONE` and so is reported
       :attr:`OwedAct.HANDOFF_READY`, not :attr:`OwedAct.EXTERNAL_BLOCKED` (see
       :attr:`OwedAct.HANDOFF_READY`).

    Pure, deterministic, zero SDK cost, no network: it never fetches merge truth
    or review threads (both the settled set and *unresolved_threads* are supplied
    by the caller) and reaches no push / merge / ready / open seam.
    """
    if pr_number in settled_pr_numbers:
        return None
    if unresolved_threads > 0:
        blocker = remediation_tracker.classify_owned_blocker(
            blocker, unresolved_threads,
        )
    ownership = remediation_tracker.classify_blocker_ownership(
        blocker, own_push=own_push,
    )
    if ownership == remediation_tracker.BLOCKER_OWNERSHIP_OWNED:
        return OwedAct.CODE_OWED
    ci_status = ci_diagnosis.classify_ci_conclusions(check_runs)
    if ci_status == "FAILING":
        return OwedAct.CODE_OWED
    if ci_status == "RUNNING":
        return OwedAct.WAIT_CI
    # ci_status == "GREEN"
    if (blocker or "").strip().upper() in _HANDOFF_GREEN_BLOCKERS:
        return OwedAct.HANDOFF_READY
    return OwedAct.EXTERNAL_BLOCKED


def check_runs_from_conclusion(conclusion: str) -> tuple[CheckRun, ...]:
    """Expand a single aggregate rollup *conclusion* into a CheckRun sequence.

    :meth:`~maestro.vcs.CodeHost.get_pr_check_conclusion` collapses the whole
    status-check rollup to one string; this rebuilds the
    :class:`~maestro.ci_diagnosis.CheckRun` vocabulary
    :func:`~maestro.ci_diagnosis.classify_ci_conclusions` reads:

    * ``"failure"`` → one failing run (the rollup concluded red);
    * ``"success"`` / ``"none"`` (no CI configured is proceed-green) → one
      success run;
    * ``""`` / ``"pending"`` (or anything unrecognised) → the empty sequence,
      which :func:`classify_ci_conclusions` reads as ``RUNNING`` (nothing
      concluded yet).

    Case- and whitespace-normalised. Pure.
    """
    normalised = (conclusion or "").strip().lower()
    if normalised == "failure":
        return (CheckRun(name="ci", conclusion="failure"),)
    if pr_facts.ci_state_clears_ci_axis(
        pr_facts.ci_state_from_conclusion(normalised),
    ):
        return (CheckRun(name="ci", conclusion="success"),)
    return ()


def derive_owed_act_blocker(
    *,
    commits_behind: int,
    check_conclusion: str,
    merge_state_status: str,
    mergeable: str,
    cleanly_mergeable: bool,
    tend_catch_up: bool = True,
) -> str:
    """Derive the owed-act *blocker* from measured PR facts. Pure.

    The single derivation both owed-act faces — the tend walk's
    ``evolution_tend._observe_owed_act`` and ``maestro pr-owed-act`` — feed to
    :func:`classify_pr_owed_act`, so the two cannot disagree on the same facts.

    Starts from :func:`~maestro.remediation_tracker.classify_merge_block` over
    the measured *commits_behind*, the rollup *check_conclusion* and the host
    *merge_state_status*, then:

    * collapses :data:`~maestro.remediation_tracker.MERGE_BLOCK_CI_PENDING` to
      :data:`~maestro.remediation_tracker.MERGE_BLOCK_NONE`
      **unconditionally** — whether the conclusion was unmeasurable (``""``) or
      genuinely in flight (``"pending"``). The CI axis is carried in full by
      :func:`check_runs_from_conclusion`, which maps both to ``RUNNING``;
      keeping the rung on the blocker axis too would swallow the conflict
      refinement below and block thread promotion (``CI_PENDING`` is not
      thread-promotable), silently downgrading a code-owed PR to ``wait-ci``;
    * refines ``NONE`` / ``BEHIND_MAIN`` / ``CI_FAILING`` to
      :data:`~maestro.remediation_tracker.MERGE_BLOCK_CONFLICTING` when the
      measured merge-tree reading says the branch no longer merges cleanly
      (*cleanly_mergeable* False) or the host reports
      ``mergeable == "CONFLICTING"``. The refinement outranks ``CI_FAILING``,
      mirroring ``classify_merge_block``'s own precedence (a conflict outranks
      CI state), so the named cause is the conflict rather than the red it is
      downstream of; the owed act is ``CODE_OWED`` either way;
    * with *tend_catch_up* False (``[loop] tend_main_merge_enabled`` off, the
      merge-queue model), resolves a surviving ``BEHIND_MAIN`` through
      :func:`~maestro.remediation_tracker.apply_landing_model`, so a
      behind-but-clean green PR reads handoff-ready rather than waiting on a
      Tend catch-up that no longer runs (#4307).

    Returns a blocker string, never an :class:`OwedAct`.
    """
    blocker = remediation_tracker.apply_landing_model(
        remediation_tracker.classify_merge_block(
            commits_behind, check_conclusion, merge_state_status,
        ),
        tend_catch_up=tend_catch_up,
        cleanly_mergeable=cleanly_mergeable,
        check_conclusion=check_conclusion,
        merge_state_status=merge_state_status,
    )
    if blocker == remediation_tracker.MERGE_BLOCK_CI_PENDING:
        blocker = remediation_tracker.MERGE_BLOCK_NONE
    conflicting = (
        not cleanly_mergeable
        or (mergeable or "").strip().upper() == "CONFLICTING"
    )
    if conflicting and blocker in (
        remediation_tracker.MERGE_BLOCK_NONE,
        remediation_tracker.MERGE_BLOCK_BEHIND_MAIN,
        remediation_tracker.MERGE_BLOCK_CI_FAILING,
    ):
        blocker = remediation_tracker.MERGE_BLOCK_CONFLICTING
    return blocker


def check_runs_from_ci_state(state: str) -> tuple[CheckRun, ...]:
    """Expand a persisted :class:`~maestro.pr_facts.CIState` into CheckRuns.

    The ``CIState``-vocabulary sibling of :func:`check_runs_from_conclusion`,
    for a caller holding the ``ci_state`` field off a persisted
    ``pr_readiness_snapshot`` rather than a host rollup conclusion:

    * ``"FAILING"`` → one failing run (the observation concluded red);
    * ``"GREEN"`` / ``"NONE"`` (no CI configured is proceed-green, matching the
      conclusion expander's ``"none"``) → one success run;
    * ``"PENDING"`` — or anything unrecognised, including ``""`` — → the empty
      sequence, which :func:`~maestro.ci_diagnosis.classify_ci_conclusions`
      reads as ``RUNNING`` (nothing concluded yet). Failing toward *the clock is
      running* is the conservative direction: an unreadable state must not
      manufacture a green or a red.

    The success branch derives from
    :func:`~maestro.pr_facts.ci_state_clears_ci_axis`, the one owner of the
    CI-axis question. Case- and whitespace-normalised. Pure.
    """
    normalised = (state or "").strip().upper()
    if normalised == "FAILING":
        return (CheckRun(name="ci", conclusion="failure"),)
    if pr_facts.ci_state_clears_ci_axis(normalised):
        return (CheckRun(name="ci", conclusion="success"),)
    return ()


def owed_act_from_readiness_snapshot(
    snapshot: Mapping[str, object],
) -> OwedAct | None:
    """Derive the owed act from a persisted ``pr_readiness_snapshot``.

    *snapshot* is the ``dataclasses.asdict`` form of a
    :class:`~maestro.pr_facts.PRReadiness` observation, as persisted on a
    :class:`~maestro.memory.TaskRecord`. Reading the owed act off that already-
    persisted fact is what makes the stand-down contradiction sensor free: no
    host call, no merge-oracle call, no SDK call.

    The fields are read **defensively**, in the tolerant style of
    :func:`~maestro.remediation_tracker._blocker_class_from_snapshot` and
    :func:`~maestro.remediation_tracker._readiness_int`, so a malformed or
    partial record never invents a blocker it cannot prove:

    1. A truthy ``settled`` short-circuits to ``None`` — a merged / closed PR is
       owed nothing. Because the fact answers this itself, no caller-supplied
       settled set and no merge-truth read is needed, and
       :func:`classify_pr_owed_act` is called with an empty settled set.
    2. ``branch_conflicting`` / ``branch_behind`` / ``awaiting_approval`` map
       back to the corresponding ``MERGE_BLOCK_*`` code in the **same precedence
       order** :meth:`~maestro.pr_facts.PRReadiness.merge_block_reason` uses
       (conflict outranks behind outranks approval-wait), falling to
       :data:`~maestro.remediation_tracker.MERGE_BLOCK_NONE`.
    3. ``ci_state`` expands through :func:`check_runs_from_ci_state`, and
       ``unresolved_threads`` is passed as the caller-supplied thread count, so
       an open thread promotes an approval-wait or clean merge state to owned
       work exactly as the ``maestro pr-owed-act`` call site's thread count
       does. Any ``unaddressed_overflow`` finding is added to that count too —
       it is code-clearable review work with no thread to reply to, and the
       snapshot records it where the CLI, reading live threads off the host, has
       no equivalent field; that is a deliberate superset of the CLI reading,
       not parity with it.

    The ``pr_number`` is irrelevant here (the settled question is answered by
    the snapshot, not by a set membership test), so ``0`` is passed. Pure, zero
    SDK cost, no network.
    """
    if snapshot.get("settled"):
        return None
    if snapshot.get("branch_conflicting"):
        blocker = remediation_tracker.MERGE_BLOCK_CONFLICTING
    elif snapshot.get("branch_behind"):
        blocker = remediation_tracker.MERGE_BLOCK_BEHIND_MAIN
    elif snapshot.get("awaiting_approval"):
        blocker = remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL
    else:
        blocker = remediation_tracker.MERGE_BLOCK_NONE
    raw_state = snapshot.get("ci_state")
    check_runs = check_runs_from_ci_state(
        raw_state if isinstance(raw_state, str) else "",
    )
    threads = _snapshot_int(snapshot, "unresolved_threads") + _snapshot_int(
        snapshot, "unaddressed_overflow",
    )
    return classify_pr_owed_act(
        0,
        blocker=blocker,
        check_runs=check_runs,
        settled_pr_numbers=frozenset(),
        unresolved_threads=threads,
    )


def _snapshot_int(snapshot: Mapping[str, object], key: str) -> int:
    """Read a non-negative int field off a persisted snapshot, defaulting 0.

    The :class:`~collections.abc.Mapping`-typed twin of
    :func:`~maestro.remediation_tracker._readiness_int`: a missing, non-int, or
    ``bool`` value (``bool`` is an ``int`` subclass, and a stray ``True`` must
    not read as one thread) degrades to ``0``, as does a negative count.
    """
    value = snapshot.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)
