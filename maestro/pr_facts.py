"""Home for the small, frozen, provenance-stamped PR-fact observations.

Maestro turns raw git/host signals into small, frozen facts that the loop reads
to make PR-servicing decisions. This module is their shared home. It holds eight
things:

* :class:`TimestampedObservation` — the freshness mixin the *stamped* facts
  inherit, so a stale observation can never author a verdict (the chokepoint
  re-observes instead of trusting an aged value). Two facts carry a wall-clock
  ``observed_at`` stamp and this test:
  :class:`MergeTruth` (merge state) and
  :class:`PRReadiness` (readiness). Both previously carried
  a byte-for-byte copy of the same ``is_fresh`` method, one edit away from
  drifting; giving the concept a single home collapses that to one definition.
* :class:`MergeTruth` plus its single fetch-first :func:`observe_pr_merge_truth`
  observer — the flagship **stamped** merge-state fact (merged / closed / open),
  provenance-stamped with the oracle that answered and *when*. It inherits
  :class:`TimestampedObservation`, so a stale observation can never author a
  verdict; the ``MaestroAgent.is_pr_settled()`` chokepoint wires the real oracles
  through the observer (memoized per iteration with a freshness TTL), and
  ``tests/test_structural_guards.py`` AST-enforces it as the only door to a
  merge decision. Pure and fully injectable — the clock, the ``fetch`` side
  effect, and every merge/close oracle are passed in.
* :class:`PRReadiness` plus its single pure :func:`observe_pr_readiness` observer
  — the aggregated **stamped** PR-readiness fact (proposal 089). It folds every
  blocker dimension the loop otherwise observes piecemeal (branch
  behind/conflicting, CI green/failing/pending, unresolved inline threads,
  unaddressed overflow findings, merge state) into one provenance-stamped
  observation deriving the single :attr:`PRReadiness.ready_and_waiting` terminal
  — the success-domain sibling of :class:`MergeTruth`. It too inherits
  :class:`TimestampedObservation` (a stale observation can never author a
  verdict) and is read through the ``MaestroAgent.observe_pr_readiness``
  chokepoint (memoized per iteration). Its :class:`CIState` /
  :func:`ci_state_from_conclusion` mapper and the branch/approval flags it derives
  compare against the module-local ``MERGE_BLOCK_*`` copies below.
* :class:`PrStaleness` plus its pure :func:`observe_pr_staleness` observer — the
  grounded, **unstamped** PR-staleness fact. It authors no durable verdict (it
  drives an idempotent, re-observed-every-lap mechanical catch-up merge), so it
  carries no freshness stamp and does *not* inherit
  :class:`TimestampedObservation`; it sits here as a sibling frozen fact.
* the **blocker-severity algebra** — the total order
  ``GREEN < THREADS_UNRESOLVED < CI_FAILING < CONFLICTING/DIRTY`` over the
  merge-blocker classes and its one pure function :func:`greenness_delta`, whose
  strictly-positive value distinguishes a real rotation toward code-clearable
  work from same-class churn. These constants are unstamped siblings of
  :class:`PrStaleness` and do *not* inherit :class:`TimestampedObservation`.
* the **live-plan projection** — :data:`LIVE_STATUSES` (the single named owner of
  the ``pending_review``/``approved``/``implemented`` live-status set), the frozen
  :class:`LivePlan` view, and the pure :func:`select_live_plans` oracle that
  projects every live plan (work in flight) into a :class:`LivePlan`. A plan is
  live or it is not, so the projection needs no freshness stamp: these are
  unstamped siblings of :class:`PrStaleness` and do *not* inherit
  :class:`TimestampedObservation`.
* the **branch projections** — the two pure owned-writable-branch oracles
  :func:`compute_owned_writable_branches` (which branches the task executor may
  push to) and :func:`verify_push_advanced_remote_head` (whether a push
  physically advanced ``origin/<branch>`` to the pushed SHA), with the module
  constants :data:`_EVOLVE_BRANCH_PREFIX` and :data:`_PROTECTED_REFS`. They take
  every git fact as a parameter (zero I/O), so — like the live-plan and severity
  siblings — they are unstamped and do *not* inherit
  :class:`TimestampedObservation`.
* the **salvage-before-disengage oracle** — :class:`Salvage` and its pure
  :func:`assess_pr_salvage` decision core ("must the loop fix-and-land this open
  own PR, or is disengagement permitted?"), plus the
  :func:`classify_pr_disposability` predicate it reads. Like the severity,
  live-plan, and branch siblings these are **unstamped** — they author no durable
  verdict — and do *not* inherit :class:`TimestampedObservation`.
  :func:`classify_pr_disposability` is the one sibling with a real runtime
  ``maestro`` dependency (it calls
  :func:`~maestro.consciousness.detect_restatement`), kept off the module-load
  path by a call-time deferred import so this base stays acyclic — see the
  leaf-module note below.

:class:`TimestampedObservation` is a **plain annotated mixin** (deliberately
*not* a ``@dataclass``): it annotates ``observed_at: float`` and defines
``is_fresh`` once, but contributes **no** field to the frozen subclasses that
inherit it, so each subclass keeps declaring its own ``observed_at`` field and
its dataclass field order is untouched. The observations, their fields, and
their freshness semantics are exactly as they were — only the duplicated method
collapses to one definition.

This is a leaf module: it imports nothing from ``maestro`` at module-load time
(typing :class:`~maestro.memory.EvolutionPlan` under ``TYPE_CHECKING`` only; its
runtime consumers — the PR-servicing faces — import *it*, never the reverse), so
it stays a clean acyclic base any fact can attach to. The one runtime ``maestro``
dependency — :func:`classify_pr_disposability`'s call to
:func:`~maestro.consciousness.detect_restatement` — is a **call-time deferred
import inside the function body**, so it adds no module-load-time import and
cannot form a cycle; a top-level ``from maestro.consciousness import`` would
instead pull that module's dependency tree (sdk, prompts, …) into this base and
falsify the acyclic invariant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.ci_diagnosis import CIStatus
    from maestro.memory import EvolutionPlan


class TimestampedObservation:
    """Mixin giving a provenance-stamped fact one shared freshness test.

    Subclasses declare ``observed_at`` as a real (dataclass) field; this mixin
    only annotates it so :meth:`is_fresh` can read ``self.observed_at`` while
    adding no field of its own to the frozen subclass.
    """

    observed_at: float

    def is_fresh(self, now: float, ttl_seconds: int) -> bool:
        """Return ``True`` while this observation may still author a verdict.

        An observation is *fresh* only while strictly less than ``ttl_seconds``
        have elapsed since :attr:`observed_at`, so no fact past its TTL drives a
        decision — the chokepoint re-observes instead of returning a stale value.

        ``ttl_seconds == 0`` makes every observation immediately stale (always
        re-observe); a clock that has not advanced past :attr:`observed_at`
        (within-iteration reuse, or harmless clock skew) is always fresh.
        """
        return (now - self.observed_at) < ttl_seconds


#: The PR's lifecycle state at observation time. ``MERGED`` and ``CLOSED`` are
#: both *settled* (no remediation is owed); only ``MERGED`` means the branch's
#: commits actually landed in ``main``.
MergeState = Literal["MERGED", "CLOSED", "OPEN"]

#: Which oracle authored the merge verdict. ``host_api`` is the authoritative
#: code-host answer; the two ``git_*`` sources are local fallbacks consulted
#: only when the host call raises — ``git_ancestry`` is a ``merge-base
#: --is-ancestor`` test, ``git_subject`` a ``git log`` subject name-drop (the
#: weakest evidence). ``version-archive`` is the *host-independent* local
#: corroboration tier: a ``VersionRecord`` (written only on an actual landing)
#: whose plan matches the PR settles it when both the linear ancestry read and
#: the host ``mergedAt`` override come up empty (a squash merge whose host read
#: is unreadable).
MergeSource = Literal[
    "host_api", "git_ancestry", "git_subject", "version-archive",
]

#: The git fallback source a :func:`observe_pr_merge_truth` ``branch_oracle``
#: represents — stamped onto :attr:`MergeTruth.source` when the host call raises
#: and the branch oracle answers.
BranchSource = Literal["git_ancestry", "git_subject"]


@dataclass(frozen=True)
class MergeTruth(TimestampedObservation):
    """One provenance-and-freshness-stamped observation of a PR's merge state.

    Frozen so a verdict cannot be mutated after the fact: a PR-servicing face
    reads the fact and makes its own decision, but never edits the observation.
    :attr:`source` records the oracle that answered and :attr:`observed_at` when
    — together they let later proposal-087 steps refuse to let a stale or
    weakly-sourced observation author a verdict. The shared freshness test
    :meth:`~maestro.pr_facts.TimestampedObservation.is_fresh` is inherited from
    :class:`maestro.pr_facts.TimestampedObservation`.
    """

    pr_number: int
    #: ``True`` when the PR is no longer open (merged *or* closed): no
    #: remediation is owed. ``False`` only for an ``OPEN`` PR.
    settled: bool
    state: MergeState
    #: ``True`` only when the PR merged — the branch's commits are in ``main``.
    #: A closed-but-unmerged PR is ``settled`` yet not ``branch_landed``.
    branch_landed: bool
    source: MergeSource
    #: Wall-clock seconds (``time.time()``-style) at observation, stamped from
    #: the injected clock. Proposal-087 step 4 keys a TTL off this.
    observed_at: float


def observe_pr_merge_truth(
    pr_number: int,
    *,
    fetch: Callable[[], None],
    host_oracle: Callable[[int], bool],
    branch_oracle: Callable[[int], bool],
    now: Callable[[], float],
    close_oracle: Callable[[int], bool] | None = None,
    branch_source: BranchSource = "git_ancestry",
    merged_at_oracle: Callable[[int], bool] | None = None,
    version_archive_oracle: Callable[[int], bool] | None = None,
) -> MergeTruth:
    """Observe one PR's merge state as a single provenance-stamped fact.

    The observation order is fixed and fetch-first, generalizing the per-call
    "fetch before any status word" ritual the PR-tending faces already follow:

    1. ``fetch()`` refreshes remote-tracking refs. It runs **first** and is
       **fail-loud** — a raising ``fetch`` propagates, never swallowed — so no
       verdict is ever authored against a stale local tree.
    2. ``host_oracle(pr_number)`` is the authoritative merge answer; when it
       returns, its verdict wins and :attr:`MergeTruth.source` is ``"host_api"``.
    3. ``branch_oracle(pr_number)`` is consulted **only when the host call
       raises** (the host API is unreachable), stamping :attr:`MergeTruth.source`
       with ``branch_source`` — the git fallback the caller wired. If the branch
       oracle also raises, that propagates: with no oracle able to answer, no
       verdict is authored.
    4. ``merged_at_oracle(pr_number)`` — the **squash-merge override**. A squash
       merge sets the host's ``mergedAt`` timestamp (the PR reads ``MERGED``) yet
       is not the branch head's linear ancestor, so a landing signal shaped like
       a ``merge-base --is-ancestor`` test returns not-merged. When the merged
       verdict from steps 2-3 is ``False`` and this oracle is wired, the observer
       consults the authoritative host ``mergedAt`` before concluding
       not-settled: a non-null ``mergedAt`` promotes the PR to ``MERGED`` /
       settled / landed, stamping :attr:`MergeTruth.source` ``"host_api"``. This
       decouples the settled verdict from whether the primary merge oracle is
       itself squash-aware. Fail-safe: a raising oracle degrades to the
       not-merged verdict already computed (never a crash, never a false
       ``MERGED``), mirroring ``close_oracle``.
    5. ``version_archive_oracle(pr_number)`` — the **local, host-independent
       corroboration tier**. It runs **only when steps 2-4 all leave the PR
       unmerged**, i.e. strictly after the fetch-first ancestry read and the
       host ``mergedAt`` override have both failed to settle. A squash merge
       whose commit is not the branch head's linear ancestor reads not-merged by
       ``--is-ancestor`` (exit 1); when the host ``mergedAt`` is also null or
       unreadable, the PR would resume its remediation lap even though its cure
       already landed. A ``VersionRecord`` is appended to ``version_archive.jsonl``
       only on an actual landing, so a matching record is a merge-truth signal
       already on local disk — this oracle reports it. A ``True`` promotes the PR
       to ``MERGED`` / settled / landed, stamping :attr:`MergeTruth.source`
       ``"version-archive"``. Fail-safe: a raising oracle degrades to the
       not-merged verdict (never a crash, never a false ``MERGED``), mirroring
       the two oracles above. The primary fetch + ancestry read are never
       replaced — this tier only adds a fallback below them.

    A merged PR is ``MERGED`` / settled / landed. An unmerged PR is ``CLOSED``
    (settled, not landed) when ``close_oracle`` is supplied and reports the PR
    closed, otherwise ``OPEN`` (not settled). ``close_oracle`` is consulted with
    the same host-first tolerance: if it raises, the PR degrades to the
    fail-safe ``OPEN`` (still owed remediation) rather than crashing the whole
    observation.

    Pure given its injected ``fetch`` / oracles / ``now`` clock, so it is
    deterministic under test; the sole consumer is the
    :meth:`MaestroAgent.is_pr_settled` chokepoint that every PR-servicing face
    reads merge state through.
    """
    fetch()
    source: MergeSource
    try:
        merged = host_oracle(pr_number)
        source = "host_api"
    except Exception:
        merged = branch_oracle(pr_number)
        source = branch_source
    if not merged and merged_at_oracle is not None:
        try:
            merged_at_set = merged_at_oracle(pr_number)
        except Exception:
            # Fail-safe: an unreachable ``mergedAt`` oracle degrades to the
            # not-merged verdict already computed — never a crashed observation
            # and never a false ``MERGED``. Mirrors the ``close_oracle``
            # typed-fallback shape (an assignment, not a log-and-fall-through).
            merged_at_set = False
        if merged_at_set:
            merged = True
            source = "host_api"
    if not merged and version_archive_oracle is not None:
        try:
            corroborated = version_archive_oracle(pr_number)
        except Exception:
            # Fail-safe: an unreadable version-archive corroboration degrades to
            # the not-merged verdict already computed — never a crashed
            # observation and never a false ``MERGED``. Mirrors the
            # ``merged_at_oracle`` / ``close_oracle`` typed-fallback shape.
            corroborated = False
        if corroborated:
            merged = True
            source = "version-archive"
    if merged:
        return MergeTruth(
            pr_number=pr_number,
            settled=True,
            state="MERGED",
            branch_landed=True,
            source=source,
            observed_at=now(),
        )
    closed = False
    if close_oracle is not None:
        try:
            closed = close_oracle(pr_number)
        except Exception:
            # Fail-safe: an unreachable close oracle degrades to OPEN (still
            # owed remediation), never a wrong CLOSED skip or a crashed
            # observation. Mirrors the host-first oracle's typed-fallback shape.
            closed = False
    return MergeTruth(
        pr_number=pr_number,
        settled=closed,
        state="CLOSED" if closed else "OPEN",
        branch_landed=False,
        source=source,
        observed_at=now(),
    )


@dataclass(frozen=True)
class PrStaleness:
    """One origin-measured observation of how stale a PR head branch is.

    Frozen so a verdict cannot be mutated after the fact — the same discipline
    :class:`maestro.pr_facts.PRReadiness` enforces. Every field is measured
    from local git against ``origin/main`` by :func:`observe_pr_staleness`;
    there is no host-API or SDK input.
    """

    #: The PR head branch this fact was observed for.
    branch: str
    #: Commits ``branch`` trails ``origin/main`` — the ``behind`` element of
    #: ``commits_ahead_behind(branch, "origin/main")``. ``0`` means up to date.
    commits_behind: int
    #: ``True`` when ``branch`` still merges into ``origin/main`` without
    #: conflict — an in-memory ``git merge-tree`` probe found no conflict.
    #: Trivially ``True`` (without probing) when ``commits_behind == 0``, since
    #: an up-to-date branch has nothing to merge.
    cleanly_mergeable: bool
    #: ``True`` iff the branch is behind ``origin/main`` **and** still merges
    #: cleanly — the one state a zero-SDK catch-up merge both needs and can
    #: perform. A behind-and-conflicting branch (``cleanly_mergeable`` False)
    #: needs the LLM conflict route, not a mechanical merge, so it is not a
    #: catch-up.
    needs_catchup: bool
    #: The ``origin/main`` head SHA this observation raced against — the base the
    #: branch's ``commits_behind`` was measured relative to (proposal 099 step
    #: 2). ``None`` when the base could not be resolved (``rev_parse`` returned
    #: ``None``). Supplied by the argument-less ``base_sha_fn`` oracle;
    #: unlike ``commits_behind`` / ``cleanly_mergeable`` it is not derived from
    #: the branch, since the base is ``origin/main`` regardless of which PR
    #: branch is observed. Pure data for now — no consumer keys on it yet.
    base_head_sha: str | None


def observe_pr_staleness(
    branch: str,
    *,
    commits_behind_fn: Callable[[str], int],
    merge_clean_fn: Callable[[str], bool],
    base_sha_fn: Callable[[], str | None],
) -> PrStaleness:
    """Observe how stale a PR head *branch* is as a single frozen fact.

    Composes the two injected git oracles into one :class:`PrStaleness`:

    * ``commits_behind_fn(branch)`` supplies :attr:`PrStaleness.commits_behind`
      — the count of commits ``branch`` trails ``origin/main`` (the ``behind``
      element of :meth:`maestro.vcs.CodeHost.commits_ahead_behind`).
    * ``merge_clean_fn(branch)`` supplies :attr:`PrStaleness.cleanly_mergeable`
      — ``True`` when ``branch`` merges into ``origin/main`` without conflict
      (``not`` :meth:`maestro.vcs.CodeHost.merge_tree_conflicts`). It is only
      consulted when ``commits_behind > 0``: a branch level with ``origin/main``
      is trivially cleanly mergeable (an empty merge cannot conflict), so the
      probe is short-circuited to avoid a wasted ``git merge-tree`` on every
      up-to-date PR.

    * ``base_sha_fn()`` supplies :attr:`PrStaleness.base_head_sha` — the
      ``origin/main`` head SHA the branch's ``commits_behind`` was measured
      against (proposal 099 step 2). Unlike the two branch-taking oracles it
      takes **no** argument: the base is ``origin/main`` regardless of which PR
      branch is observed. Best-effort — ``None`` when the base cannot be
      resolved.

    :attr:`PrStaleness.needs_catchup` is derived as ``commits_behind > 0 and
    cleanly_mergeable``: a behind, cleanly-mergeable branch is exactly the case a
    mechanical ``merge_from("origin/main")`` catch-up both needs and can perform.

    The two behind/merge oracles take only the branch name and the base oracle
    takes no argument; the observer holds no ref-string knowledge — the
    ``origin/main`` comparand lives in the
    ``EvolutionEngine._observe_pr_staleness`` adapter. Pure given its injected
    oracles, so it is deterministic under test.
    """
    commits_behind = commits_behind_fn(branch)
    # A branch level with ``origin/main`` (``commits_behind == 0``) merges
    # trivially — an empty merge cannot conflict — so it is cleanly mergeable by
    # definition, and no consumer reads ``cleanly_mergeable`` when there is
    # nothing to catch up (both ``needs_catchup`` below and the tend
    # conflict-route decision require ``commits_behind > 0``). Short-circuit the
    # in-memory ``git merge-tree`` probe in that case rather than paying it on
    # every up-to-date PR each tend lap.
    cleanly_mergeable = commits_behind == 0 or merge_clean_fn(branch)
    needs_catchup = commits_behind > 0 and cleanly_mergeable
    return PrStaleness(
        branch=branch,
        commits_behind=commits_behind,
        cleanly_mergeable=cleanly_mergeable,
        needs_catchup=needs_catchup,
        base_head_sha=base_sha_fn(),
    )


# --- Blocker-severity total order — the mechanical spine of "strictly greener" ---
#
# The bind-the-act-to-blocker-green rule requires that a re-routed cure's new
# blocker be *strictly greener* than the old one, and the
# ``blocker-rotation-not-clearance`` limitation is the exact gap it closes:
# nothing computed greenness, so a 40-lap ``CONFLICTING → CONFLICTING`` churn (a
# genuine stall) and a real ``CONFLICTING → CI_FAILING`` rotation *toward*
# code-clearable work read as the same undifferentiated "partial, awaiting
# merge."
#
# The severity algebra below supplies the missing total order over the
# merge-blocker classes the loop already names, from greenest (clear) to reddest
# (conflicting):
#
#     GREEN  <  THREADS_UNRESOLVED  <  CI_FAILING  <  CONFLICTING / DIRTY
#
# and one pure function over it — ``greenness_delta`` (signed severity movement:
# positive when the blocker moved toward clear). It is a self-contained severity
# algebra: it knows nothing about readiness snapshots, task history, or git —
# callers map their own state onto the class strings and ask. Pure,
# deterministic, zero SDK cost. Unstamped siblings of :class:`PrStaleness`, none
# of these touch :class:`TimestampedObservation`.

#: The merge-blocker severity classes, greenest first. The string values are the
#: canonical ``MERGE_BLOCK_*`` codes (see :mod:`maestro.remediation_tracker`) so a
#: caller holding a classified blocker code can pass it straight through. ``GREEN``
#: names the cleared state (no code-clearable blocker) that
#: :data:`~maestro.remediation_tracker.MERGE_BLOCK_NONE` represents.
GREEN = "GREEN"
THREADS_UNRESOLVED = "THREADS_UNRESOLVED"
CI_FAILING = "CI_FAILING"
CONFLICTING = "CONFLICTING"
#: The raw ``mergeable_state`` GitHub reports for a conflicting branch; it maps to
#: the same severity as :data:`CONFLICTING` so a caller reading either the
#: classified code or the raw host state agrees.
DIRTY = "DIRTY"

#: Severity rank per blocker class — lower is greener. The load-bearing total
#: order: ``GREEN`` (cleared) is 0; a conflicting/dirty branch (the reddest,
#: needing LLM conflict resolution before any CI result is meaningful) is the max.
_SEVERITY: dict[str, int] = {
    GREEN: 0,
    THREADS_UNRESOLVED: 1,
    CI_FAILING: 2,
    CONFLICTING: 3,
    DIRTY: 3,
}

#: Severity assigned to an unrecognised class. An unknown blocker is treated as
#: maximally red (never greener than a known one), so a mislabelled or novel
#: blocker can never be mistaken for progress toward clear.
_MAX_SEVERITY = max(_SEVERITY.values())


def _severity(blocker_class: str) -> int:
    """Return the severity rank of *blocker_class* (lower is greener).

    Casing and surrounding whitespace are normalised before lookup, so a raw
    ``" dirty "`` ranks the same as the canonical :data:`DIRTY`, mirroring the
    :func:`maestro.remediation_tracker.classify_blocker_ownership` convention.
    An unrecognised class defaults to :data:`_MAX_SEVERITY` — treated as the
    reddest, never greener than a known blocker. Pure.
    """
    return _SEVERITY.get((blocker_class or "").strip().upper(), _MAX_SEVERITY)


def greenness_delta(before: str, after: str) -> int:
    """Return the signed greenness movement from *before* to *after*.

    ``severity(before) - severity(after)``: **positive** when the blocker moved
    toward clear (got greener — e.g. ``CONFLICTING → CI_FAILING`` yields ``+1``),
    zero when it stayed in the same severity class (``CONFLICTING → CONFLICTING``),
    and negative on a regression (``CI_FAILING → CONFLICTING`` yields ``-1``).
    A **strictly positive** delta is the mechanical form of the "the new blocker
    must be strictly greener than the old" clause — the blocker moved down the
    total order toward ``GREEN``. Pure, deterministic, zero SDK cost.
    """
    return _severity(before) - _severity(after)


# --- Live-plan projection — one pure owner of "which plans are live right now?" ---
#
# The loop mints new evolution plans at submission time, and several guards try to
# stop it from restating work already in flight. Those guards read one corpus —
# the *live* plans, whose status is ``pending_review``, ``approved``, or
# ``implemented`` — with :data:`LIVE_STATUSES` its single named owner and
# :func:`select_live_plans` projecting each into a frozen :class:`LivePlan`. Pure,
# deterministic, zero-I/O: it observes nothing, imports nothing from
# :mod:`maestro.memory` at module-load time (typing
# :class:`~maestro.memory.EvolutionPlan` under ``TYPE_CHECKING`` only), and derives
# :attr:`LivePlan.pr_number` with a
# tolerant inline parse rather than depending on a raising helper. Unstamped
# siblings of :class:`PrStaleness` — a plan is live or it is not — so none of these
# inherit :class:`TimestampedObservation`.

#: The three plan statuses that count as *live* (work in flight). A live plan
#: carries exactly one of these, so the value is itself the provenance stamp of
#: which status admitted it — mirroring :data:`maestro.pr_facts.MergeSource`.
LiveStatus = Literal["pending_review", "approved", "implemented"]

#: The live statuses as a tuple — the single named owner of "which statuses count
#: as live". Every consumer (this module's :func:`select_live_plans`,
#: :meth:`maestro.memory.Memory.get_live_plan_records`) reads membership from here
#: rather than re-listing the three statuses inline.
LIVE_STATUSES: tuple[LiveStatus, ...] = (
    "pending_review",
    "approved",
    "implemented",
)


@dataclass(frozen=True)
class LivePlan:
    """One live plan and the PR (if any) backing it.

    Frozen so a caller cannot mutate the projection: the oracle answers "this
    plan is live" and each guard makes its own decision, but never edits the
    fact. :attr:`status` is the status that admitted the plan — the provenance
    stamp — and :attr:`pr_number` is the tolerantly-parsed number behind
    :attr:`pr_url`, ``None`` when no PR is attached or the URL is unparseable.
    """

    plan_id: str
    status: LiveStatus
    title: str
    rationale: str
    pr_url: str | None
    pr_number: int | None


def _pr_number_from_url(pr_url: str | None) -> int | None:
    """Extract a positive PR number from *pr_url*, tolerantly.

    Unlike :meth:`maestro.evolution.Evolution._pr_number_from_url`, which raises
    on a missing or malformed URL, this returns ``None`` for anything it cannot
    parse into a positive integer so :func:`select_live_plans` stays pure and
    total.
    """
    if not pr_url:
        return None
    tail = pr_url.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
    try:
        number = int(tail)
    except ValueError:
        return None
    return number if number > 0 else None


def select_live_plans(plans: list[EvolutionPlan]) -> list[LivePlan]:
    """Project every *live* plan in *plans* into a :class:`LivePlan`.

    A plan is live when its status is ``pending_review``, ``approved``, or
    ``implemented``; ``merged``/``rejected``/``failed``/``obsolete`` plans are
    terminal and excluded. Order is preserved. Pure — no I/O, no SDK, no host
    calls.
    """
    live: list[LivePlan] = []
    for plan in plans:
        status = plan.status
        if status not in LIVE_STATUSES:
            continue
        live.append(
            LivePlan(
                plan_id=plan.plan_id,
                status=status,
                title=plan.title,
                rationale=plan.rationale,
                pr_url=plan.pr_url,
                pr_number=_pr_number_from_url(plan.pr_url),
            )
        )
    return live


# --- Branch projections — the two owned-writable-branch git facts (proposal 093) ---
#
# The action gate binds ``git push`` permission to a single branch string — the
# branch the run launched on — which encodes *which branch the run started on*,
# not *whose PR this is*. These two pure functions isolate the durable answers:
# :func:`compute_owned_writable_branches` decides a branch is owned-writable when
# it is the run's own branch, or the ``maestro/evolve/*`` head of one of the
# agent's own open evolution PRs (confirmed by *both* the live open-PR set and the
# persistent PR ledger), and never ``main``/``master``; the action gate reads that
# write set. :func:`verify_push_advanced_remote_head` binds a delivery claim to a
# physical ``origin/<branch>`` advance; the push-delivery path reads that
# verification. Both keep all git I/O in injected seams, so they stay pure and
# directly unit-testable — unstamped siblings of :class:`PrStaleness`, the live-plan
# projection, and the severity algebra that do *not* inherit
# :class:`TimestampedObservation`.

#: Prefix marking a branch as an evolution-PR head — mirrors the shape produced
#: by :func:`maestro.vcs.evolution_branch`. Provenance lives in the name.
_EVOLVE_BRANCH_PREFIX = "maestro/evolve/"

#: Refs the task executor may never write to, regardless of provenance. Kept as
#: an independent copy of :data:`maestro.tool_gating._PROTECTED_REFS` so the
#: provenance layer and the enforcement gate can never silently widen one
#: another.
_PROTECTED_REFS: frozenset[str] = frozenset({"main", "master"})


def compute_owned_writable_branches(
    current_branch: str,
    open_pr_branches: Iterable[str],
    pr_ledger_branches: Iterable[str],
) -> frozenset[str]:
    """Compute the provenance-stamped set of branches the executor may write to.

    A branch is owned-writable when it is either the run's own
    ``current_branch`` (included only when truthy) or a ``maestro/evolve/*``
    head confirmed by appearing in *both* ``open_pr_branches`` (the live open-PR
    set) and ``pr_ledger_branches`` (the persistent PR ledger). The protected
    refs (:data:`_PROTECTED_REFS`) are subtracted unconditionally at the end, so
    ``main``/``master`` can never appear however they entered.

    All facts are parameters — the caller supplies the live open-PR set and the
    ledger branches — so the function stays pure and directly unit-testable.
    """
    writable: set[str] = set()
    if current_branch:
        writable.add(current_branch)
    confirmed = set(open_pr_branches) & set(pr_ledger_branches)
    writable.update(
        branch for branch in confirmed if branch.startswith(_EVOLVE_BRANCH_PREFIX)
    )
    return frozenset(writable - _PROTECTED_REFS)


def verify_push_advanced_remote_head(
    branch: str,
    expected_sha: str,
    *,
    fetch: Callable[[str], bool],
    read_remote_head: Callable[[str], str | None],
) -> bool:
    """Return True only when ``origin/<branch>`` physically advanced to *expected_sha*.

    The push-act analogue of ``memory.verify_obsolete_persisted``: a physical
    read of remote state bound to the delivery claim, performed *this turn*,
    synchronous with the push. A ``git push`` subprocess exiting 0 does **not**
    prove the remote branch head moved — an already-up-to-date push, or a push
    the loop's own permission layer no-op'd, exits 0 while ``origin/<branch>``
    stays put. This function refuses to trust that raw return: it re-reads the
    remote head and compares.

    ``fetch(branch)`` refreshes the remote-tracking ref for *branch*; a falsey
    return (fetch failure) short-circuits to ``False`` without a read.
    ``read_remote_head(branch)`` returns the fetched ``origin/<branch>`` head
    SHA, or ``None`` on a read failure. The function returns ``True`` iff the
    fetch succeeded *and* the read head SHA equals *expected_sha* — so a no-op
    push (unchanged remote head → SHA mismatch), a fetch failure, a read
    failure, or a missing/empty *expected_sha* all return ``False``. 'Delivered'
    can therefore never be stamped on a push that did not move the head.

    Pure — all git I/O is injected via *fetch* / *read_remote_head*, so it is
    directly unit-testable with no subprocess.
    """
    if not expected_sha:
        return False
    if not fetch(branch):
        return False
    remote_head = read_remote_head(branch)
    if remote_head is None:
        return False
    return remote_head == expected_sha


# --- Merge-block reason codes — independent copies for the readiness fact ---
#
# :func:`observe_pr_readiness` derives its branch/approval flags by comparing an
# injected merge-block reason string against these four codes. Their canonical
# definitions live in :mod:`maestro.remediation_tracker`, but that module imports
# its blocker vocabulary (``GREEN`` / ``CI_FAILING`` / ...) *from* this one, so
# importing the codes back would form a cycle and break the leaf invariant. These
# are therefore independent string-literal copies — the same idiom as the
# severity-class constants and :data:`_PROTECTED_REFS` above — pinned to the
# canonical values by an agreement test in ``tests/test_pr_facts.py`` so the two
# homes can never silently drift.

#: An injected merge-block reason equal to this marks the branch as trailing
#: ``origin/main`` without a conflict — canonical
#: :data:`maestro.remediation_tracker.MERGE_BLOCK_BEHIND_MAIN`.
MERGE_BLOCK_BEHIND_MAIN = "BEHIND_MAIN"
#: ... marks the branch as conflicting with its base — canonical
#: :data:`maestro.remediation_tracker.MERGE_BLOCK_CONFLICTING`.
MERGE_BLOCK_CONFLICTING = "CONFLICTING"
#: ... marks outstanding review approval as the sole host-side merge blocker —
#: canonical :data:`maestro.remediation_tracker.MERGE_BLOCK_AWAITING_APPROVAL`.
MERGE_BLOCK_AWAITING_APPROVAL = "AWAITING_APPROVAL"
#: ... marks that no code-clearable merge blocker remains — canonical
#: :data:`maestro.remediation_tracker.MERGE_BLOCK_NONE`.
MERGE_BLOCK_NONE = "NONE"

# --- The finer CI/review merge-blocker labels ---
#
# :func:`classify_merge_blocker_label` returns one of these three honest labels
# in place of the flat ``CI_PENDING`` rollup costume. Their canonical homes live
# in :mod:`maestro.remediation_tracker`, but this leaf module cannot import that
# module back without a cycle, so — like the ``MERGE_BLOCK_*`` copies above —
# they are independent string-literal copies pinned to the canon by an agreement
# test in ``tests/test_pr_facts.py``.

#: ... marks a concluded CI *failure* (a concrete fix target) — canonical
#: :data:`maestro.remediation_tracker.MERGE_BLOCK_CI_FAILING`.
MERGE_BLOCK_CI_FAILING = "CI_FAILING"
#: ... marks a still-*running* CI (queued / in-progress, nothing failed) — a
#: not-mine-this-turn wait to hold on. Canonical
#: :data:`maestro.remediation_tracker.MERGE_BLOCK_CI_RUNNING`.
MERGE_BLOCK_CI_RUNNING = "CI_RUNNING"
#: ... marks a green PR still awaiting review approval — a review wait handed to
#: Thomas, distinct from a CI wait. Canonical
#: :data:`maestro.remediation_tracker.MERGE_BLOCK_AWAITING_REVIEW`.
MERGE_BLOCK_AWAITING_REVIEW = "AWAITING_REVIEW"


# --- Aggregated PR-readiness fact ---
#
# While Maestro's PR is open the loop decides "is there still remediation work I
# can do on this PR?" at several places, each observing a *single* blocker
# dimension for itself. :class:`PRReadiness` makes the terminal an observed fact:
# it aggregates every blocker dimension the loop already observes piecemeal into
# one provenance-stamped observation deriving a single
# :attr:`PRReadiness.ready_and_waiting` boolean — the success-domain sibling of
# :class:`MergeTruth`, produced by exactly one pure, fully-injectable observer,
# :func:`observe_pr_readiness`. It changes only *how the loop reads* whether a PR
# is ready and waiting, never how it writes anything, and is consumed through the
# ``MaestroAgent.observe_pr_readiness`` chokepoint.

#: The PR's aggregate CI state at observation time. ``GREEN`` means the
#: status-check rollup concluded success, ``FAILING`` it concluded failure,
#: ``PENDING`` it has not concluded, and ``NONE`` no checks exist. Whether a state
#: clears the CI dimension is owned by :func:`ci_state_clears_ci_axis`.
CIState = Literal["GREEN", "FAILING", "PENDING", "NONE"]


def ci_state_from_conclusion(conclusion: str) -> CIState:
    """Map a host status-check rollup *conclusion* to a :class:`CIState`.

    Translates the strings :meth:`maestro.vcs.CodeHost.get_pr_check_conclusion`
    returns — ``"success"`` / ``"failure"`` / ``"none"`` / ``""`` (pending) — into
    the four-valued aggregate CI state the readiness fact uses. Anything other
    than the three named conclusions (including the empty pending string) is
    treated as ``PENDING`` so an unconcluded rollup never reads as ready. Pure.
    """
    normalized = (conclusion or "").strip().lower()
    if normalized == "success":
        return "GREEN"
    if normalized == "failure":
        return "FAILING"
    if normalized == "none":
        return "NONE"
    return "PENDING"


def ci_state_clears_ci_axis(state: str) -> bool:
    """Return whether a :class:`CIState` leaves nothing to wait on in CI.

    The single owner of the CI-axis question: ``GREEN`` (every check concluded
    success) and ``NONE`` (no checks configured — proceed, per the no-CI work
    target contract) clear it; ``FAILING``, ``PENDING`` and anything
    unrecognised do not. :func:`observe_pr_readiness`,
    :func:`classify_merge_blocker_label` and the owed-act CheckRun expanders in
    :mod:`maestro.pr_owed_act` all derive from it, so ``ready_and_waiting`` and a
    ``HANDOFF_READY`` owed act cannot disagree on a no-CI PR. Takes a plain
    :class:`str`; case- and whitespace-normalised. Pure.

    Accepted trade-off: on a CI-backed repo the host reports an empty status
    rollup (``NONE``) in the short window after a push before any check
    registers, so a readiness read taken in that window clears the CI axis
    too. The alternative — treating ``NONE`` as pending — would strand every
    genuinely no-CI PR forever, which is the defect this owner fixes (#4133).
    """
    return (state or "").strip().upper() in ("GREEN", "NONE")


#: Which oracle family authored the aggregate observation. ``host_api`` — every
#: dimension came from the authoritative code-host API (the merge-truth oracle
#: answered from the host). ``mixed`` — merge state fell back to a local git
#: oracle while CI / threads / overflow still came from the host. ``git`` is
#: reserved for a future all-git observation path and is not produced today.
ReadinessSource = Literal["host_api", "git", "mixed"]


@dataclass(frozen=True)
class PRReadiness(TimestampedObservation):
    """One provenance-and-freshness-stamped observation of a PR's readiness.

    Frozen so a verdict cannot be mutated after the fact: a PR-servicing face
    reads the fact and makes its own decision, but never edits the observation —
    the same discipline :class:`MergeTruth` enforces.
    :attr:`source` records the oracle family that answered and :attr:`observed_at`
    when, so a later step can refuse to let a stale observation author a verdict.
    The shared freshness test
    :meth:`~maestro.pr_facts.TimestampedObservation.is_fresh` is inherited from
    :class:`maestro.pr_facts.TimestampedObservation`.

    :attr:`ready_and_waiting` is the single honest terminal: the PR is open and
    every *code-clearable* blocker is clear, so the sole remaining gate is
    Thomas's approval (or Tend's mechanical push) — the one state in which
    standing down on the PR is completed work, not avoidance.
    """

    pr_number: int
    #: ``True`` when the PR's branch has landed in ``main`` (merged / squashed /
    #: ancestor of ``origin/main``) — the reported *merged* snapshot field, not a
    #: term in the :attr:`ready_and_waiting` derivation. Openness is answered by
    #: :attr:`settled` (merged *or* closed), which a landed PR always sets too;
    #: reading ``branch_landed`` for openness is exactly the half-answer that let
    #: a human-closed, unmerged PR read as waiting on Thomas.
    branch_landed: bool
    #: ``True`` when the branch trails ``origin/main`` without a conflict — a
    #: code-clearable blocker (catch up, then CI on the merged tree is meaningful).
    branch_behind: bool
    #: ``True`` when the branch conflicts with its base — a code-clearable blocker
    #: needing LLM conflict resolution.
    branch_conflicting: bool
    ci_state: CIState
    #: Count of unresolved, eligible inline review threads (🐛/💡/✏️/human)
    #: lacking a terminal reply — each a code-clearable unit of work.
    unresolved_threads: int
    #: Count of body-overflow findings not yet acknowledged — each a
    #: code-clearable unit of work with no thread to reply to.
    unaddressed_overflow: int
    #: ``True`` when the sole host-side merge blocker is outstanding review
    #: approval (``mergeable_state`` BLOCKED). Informational provenance: an
    #: approval wait is exactly the external gate ready-and-waiting hands to
    #: Thomas, so it never *blocks* readiness on its own.
    awaiting_approval: bool
    #: ``True`` iff the PR is open (:attr:`settled` is ``False`` — neither merged
    #: nor closed) and no code-clearable blocker remains — the derived terminal
    #: the dequeue gate keys on.
    ready_and_waiting: bool
    source: ReadinessSource
    #: Wall-clock seconds (``time.time()``-style) at observation, stamped from
    #: the injected clock. Keyed by a TTL the way :class:`MergeTruth` is.
    observed_at: float
    #: ``True`` when the PR is no longer open — merged *or* closed, so no
    #: remediation is owed. Mirrors :attr:`MergeTruth.settled`, the field that
    #: actually answers the *open* question :attr:`branch_landed` only
    #: half-answers (a human-closed, unmerged PR is settled yet not landed).
    #: Defaults to ``False`` — the open reading — so a constructor that does not
    #: set it fails safe toward "still owed work".
    settled: bool = False

    def merge_block_reason(self) -> str:
        """Recover the single merge-block reason code from the branch flags.

        The inverse of the derivation :func:`observe_pr_readiness` runs on the
        ``merge_block_oracle`` reason: it maps the mutually-exclusive
        :attr:`branch_conflicting` / :attr:`branch_behind` /
        :attr:`awaiting_approval` flags back to the corresponding
        :data:`~maestro.remediation_tracker.MERGE_BLOCK_*` code — in the same
        priority order (conflict outranks behind outranks approval-wait) — and
        returns :data:`~maestro.remediation_tracker.MERGE_BLOCK_NONE` when none
        is set. Lets a caller read the blocker code off the readiness fact
        without re-consulting the oracle; the single source of this mapping for
        every face that would otherwise re-inline the if/elif chain. Pure.
        """
        if self.branch_conflicting:
            return MERGE_BLOCK_CONFLICTING
        if self.branch_behind:
            return MERGE_BLOCK_BEHIND_MAIN
        if self.awaiting_approval:
            return MERGE_BLOCK_AWAITING_APPROVAL
        return MERGE_BLOCK_NONE


def observe_pr_readiness(
    pr_number: int,
    *,
    merge_truth: Callable[[int], MergeTruth],
    ci_oracle: Callable[[int], CIState],
    thread_oracle: Callable[[int], int],
    overflow_oracle: Callable[[int], int],
    merge_block_oracle: Callable[[int], str],
    now: Callable[[], float],
) -> PRReadiness:
    """Observe one PR's readiness as a single provenance-stamped fact.

    Composes the injected oracles — each already an observer the loop runs
    piecemeal today — into one :class:`PRReadiness`:

    * ``merge_truth(pr)`` supplies :attr:`PRReadiness.branch_landed` and
      :attr:`PRReadiness.settled` (and its
      :attr:`~maestro.pr_facts.MergeTruth.source` decides this fact's
      provenance: ``host_api`` when the host answered, else ``mixed``).
    * ``ci_oracle(pr)`` supplies the aggregate :class:`CIState`.
    * ``thread_oracle(pr)`` / ``overflow_oracle(pr)`` supply the counts of
      unresolved inline threads / unaddressed overflow findings.
    * ``merge_block_oracle(pr)`` supplies the deterministic
      :func:`~maestro.remediation_tracker.classify_merge_block` reason code,
      from which ``branch_behind`` / ``branch_conflicting`` / ``awaiting_approval``
      are derived.

    :attr:`PRReadiness.ready_and_waiting` is ``True`` iff the PR is open
    (``not settled`` — merged *or* closed leaves nothing waiting; a human-closed
    unmerged PR is settled without being landed, so ``branch_landed`` alone would
    read it as open) **and** no code-clearable blocker remains:
    ``not branch_behind``, ``not branch_conflicting``, ``ci_state_clears_ci_axis(ci_state)``,
    ``unresolved_threads == 0``, ``unaddressed_overflow == 0``. An approval wait
    is the external gate this fact hands to Thomas, so it never blocks readiness.
    ``branch_landed`` stays on the fact as the reported *merged* snapshot field.

    Pure given its injected oracles and ``now`` clock, so it is deterministic
    under test and consumed by nobody yet.
    """
    truth = merge_truth(pr_number)
    ci_state = ci_oracle(pr_number)
    unresolved_threads = thread_oracle(pr_number)
    unaddressed_overflow = overflow_oracle(pr_number)
    reason = merge_block_oracle(pr_number)
    branch_landed = truth.branch_landed
    branch_behind = reason == MERGE_BLOCK_BEHIND_MAIN
    branch_conflicting = reason == MERGE_BLOCK_CONFLICTING
    awaiting_approval = reason == MERGE_BLOCK_AWAITING_APPROVAL
    ready_and_waiting = (
        not truth.settled
        and not branch_behind
        and not branch_conflicting
        and ci_state_clears_ci_axis(ci_state)
        and unresolved_threads == 0
        and unaddressed_overflow == 0
    )
    source: ReadinessSource = (
        "host_api" if truth.source == "host_api" else "mixed"
    )
    return PRReadiness(
        pr_number=pr_number,
        branch_landed=branch_landed,
        branch_behind=branch_behind,
        branch_conflicting=branch_conflicting,
        ci_state=ci_state,
        unresolved_threads=unresolved_threads,
        unaddressed_overflow=unaddressed_overflow,
        awaiting_approval=awaiting_approval,
        ready_and_waiting=ready_and_waiting,
        source=source,
        observed_at=now(),
        settled=truth.settled,
    )


def classify_merge_blocker_label(
    readiness: PRReadiness, ci_conclusions: CIStatus,
) -> str:
    """Split the flat ``CI_PENDING`` merge-blocker costume into the honest label.

    A serviced-but-unmerged own PR used to record one flat ``CI_PENDING`` string
    for two states whose correct response differs — a running-CI wait (hold on)
    and a review wait (hand to Thomas) — plus a genuine CI failure. This pure
    classifier reads the already-observed :class:`PRReadiness` fact (its CI-rollup
    :attr:`~PRReadiness.ci_state` and its ``reviewDecision`` proxy
    :attr:`~PRReadiness.awaiting_approval`) together with the per-check
    :func:`~maestro.ci_diagnosis.classify_ci_conclusions` verdict and returns
    exactly one honest label:

    * :data:`MERGE_BLOCK_CI_FAILING` — at least one check concluded failing:
      either the rollup itself is ``FAILING`` or a concretely-named per-check
      failure (``ci_conclusions == "FAILING"``) that recovers a timing skew in
      which the aggregate rollup still reads pending;
    * :data:`MERGE_BLOCK_AWAITING_REVIEW` — CI leaves nothing to wait on (every
      check concluded success, ``ci_state == "GREEN"``, *or* no checks exist at
      all, ``ci_state == "NONE"``) yet review is still outstanding
      (``awaiting_approval``): a review wait handed to Thomas, never a CI wait;
    * :data:`MERGE_BLOCK_CI_RUNNING` — otherwise: a check is queued / in progress
      and none has failed — an honestly not-mine-this-turn wait to hold on.

    When CI leaves nothing to wait on (``GREEN`` or ``NONE``) **and** review is
    already approved no host-side blocker remains, so it returns
    :data:`MERGE_BLOCK_NONE` (the boundary the caller keeps distinct from a real
    blocker). ``NONE`` (no checks configured) is grouped with ``GREEN`` because a
    PR with no CI can never be a CI wait — collapsing it into ``CI_RUNNING`` would
    assert a queued check that does not exist, re-conflating a review-wait with a
    CI-wait. Pure: reads only the injected fact and verdict and makes no host call
    of its own.
    """
    if readiness.ci_state == "FAILING" or ci_conclusions == "FAILING":
        return MERGE_BLOCK_CI_FAILING
    if ci_state_clears_ci_axis(readiness.ci_state):
        if readiness.awaiting_approval:
            return MERGE_BLOCK_AWAITING_REVIEW
        return MERGE_BLOCK_NONE
    return MERGE_BLOCK_CI_RUNNING


# --- Salvage-before-disengage oracle — the single veto over give-up (094, 106) ---
#
# "Must the loop fix-and-land this open own PR, or is disengagement permitted?" is
# answered here: the pure :func:`assess_pr_salvage` decision core over the
# already-observed :class:`PRReadiness` fact plus a disposability predicate. Every
# give-up actuator clears it through the agent's ``_assess_pr_salvage`` seam
# (AST-enforced as the sole door by ``tests/test_structural_guards.py``). Like
# the severity, live-plan, and branch siblings above it authors no durable verdict,
# so it is **unstamped** and does not inherit :class:`TimestampedObservation`.
# :func:`classify_pr_disposability` is the one sibling that consumes a real runtime
# ``maestro`` primitive (:func:`~maestro.consciousness.detect_restatement`); it
# reaches it via a call-time deferred import to keep this leaf base acyclic (see the
# module docstring's leaf-module note), and copy-pins the Jaccard threshold below.

#: Commits a CONFLICTING/DIRTY own PR may trail ``origin/main`` before reconciling
#: it becomes a fiction. Below (and at) this budget the branch is close enough to
#: ``main`` that an LLM conflict-resolve can land it before it re-conflicts, so
#: the oracle demands salvage; measured strictly past it, the branch
#: re-conflicts before it can land — salvage is a treadmill and disengagement
#: (``gh pr close``) is the honest move. Threading the *behind* count through
#: :func:`assess_pr_salvage` turns Maestro's own staleness measurement from an
#: alibi for narrated-but-unlanded reconciles into a disposal trigger.
RECONCILE_COMMITS_BEHIND_BUDGET = 10


# --- Stalled-remediation lap-cap copy-pin — 099 leaf-preserving dependency ---
#
# :func:`assess_pr_salvage`'s behind-budget disengage arm no longer disposes on
# distance alone (proposal 099): it also requires the PR to have failed this many
# consecutive laps against a base that stood still. The cap's canonical home is
# :data:`maestro.loop.pr_remediation.STALLED_REMEDIATION_LAP_CAP`, but the loop package is the
# heavy orchestrator — importing it into this leaf base would drag its dependency
# tree in and form an import cycle (the same reasoning as the
# ``RESTATEMENT_THRESHOLD`` / ``MERGE_BLOCK_*`` copies). So the value is
# copy-pinned here and the two homes are held equal by an agreement test in
# ``tests/test_pr_facts.py``.
_STALLED_REMEDIATION_LAP_CAP: int = 2

# Sentinel distinguishing "``same_base_failures`` argument omitted" (the flag-off
# path — reproduce the legacy distance-behind disengage, base-blind) from an
# explicit ``None`` (an unmeasurable same-base streak — never dispose on a guess).
# Mirrors :data:`maestro.memory._UNSET`.
_UNSET: object = object()


class Salvage(Enum):
    """The two verdicts :func:`assess_pr_salvage` returns for an open own PR.

    Mirrors the named-state style of
    :class:`~maestro.pr_remediation_state.PrRemediationState`.
    :attr:`SALVAGE_AND_LAND` is the demand to reconcile the branch and land it —
    the work is unfinished (not ready-and-waiting) and worth keeping (not
    disposable); :attr:`DISENGAGE_PERMITTED` says standing down, closing, or
    retiring is an honest completion rather than avoidance.
    """

    #: The loop must fix-and-land this PR: a code-clearable blocker remains and
    #: the work is unique and valuable.
    SALVAGE_AND_LAND = "salvage_and_land"
    #: Disengagement is permitted: the PR is done and only a human's approval is
    #: missing, or its work is genuinely disposable.
    DISENGAGE_PERMITTED = "disengage_permitted"


def _base_stall_permits_disengage(same_base_failures: object) -> bool:
    """Does the same-base failure streak permit behind-budget disengage? (099).

    The three-way ``same_base_failures`` protocol threaded through
    :func:`assess_pr_salvage`:

    * :data:`_UNSET` (argument omitted, the flag-off path) → ``True``: reproduce
      the legacy distance-behind disengage exactly, base-blind.
    * ``None`` (an explicit unmeasurable streak) → ``False``: never dispose on a
      guess — fail safe toward salvage.
    * an ``int`` → ``True`` iff the streak has reached
      :data:`_STALLED_REMEDIATION_LAP_CAP` — the PR has failed that many
      consecutive laps against a base that stood still. Below the cap the moved
      base is a new race the branch has never been re-run against, so salvage is
      still owed even when it is far behind.

    Any other object fails safe to ``False`` (toward salvage). Pure, zero cost.
    """
    if same_base_failures is _UNSET:
        return True
    if not isinstance(same_base_failures, int):
        return False
    return same_base_failures >= _STALLED_REMEDIATION_LAP_CAP


def assess_pr_salvage(
    readiness: PRReadiness,
    disposable: bool,
    *,
    commits_behind: int | None = None,
    same_base_failures: object = _UNSET,
    code_owed: object = _UNSET,
) -> Salvage:
    """Answer "salvage-and-land, or is disengagement permitted?" — purely.

    Returns :attr:`Salvage.SALVAGE_AND_LAND` iff the PR is *not* ready-and-waiting
    (:attr:`PRReadiness.ready_and_waiting` is ``False`` — a code-clearable blocker
    such as a conflict, a behind-main branch, failing CI, an unresolved thread, or
    unaddressed overflow still stands) *and* the work is *not* disposable. Every
    other case — the PR is done and only a human's approval is missing, or the
    work is disposable — returns :attr:`Salvage.DISENGAGE_PERMITTED`.

    **Settled short-circuit.** A settled PR (:attr:`PRReadiness.settled` — merged
    *or* closed) returns :attr:`Salvage.DISENGAGE_PERMITTED` ahead of every test
    below: there is no landing left to salvage. This keeps one coherent settled
    fact with :func:`observe_pr_readiness`, whose openness test is ``not
    settled`` — without it a human-closed, unmerged PR would read *not*
    ready-and-waiting, resolve to :attr:`SALVAGE_AND_LAND`, and — as the sole
    veto over every give-up actuator — block its own stand-down writers and the
    orphaned-cure sweep forever. It lives inside this pure oracle rather than at
    the ``_assess_pr_salvage`` seam or its callers so the single AST-enforced
    give-up door stays single.

    **Budget-aware disposal.** Even on non-disposable, not-ready work, a
    *CONFLICTING/DIRTY* blocker (:attr:`PRReadiness.branch_conflicting`) whose
    branch is measured strictly past :data:`RECONCILE_COMMITS_BEHIND_BUDGET`
    commits behind ``origin/main`` resolves to :attr:`Salvage.DISENGAGE_PERMITTED`
    instead: such a branch re-conflicts before an LLM reconcile can land it, so
    "salvage-and-land" is a treadmill and closing is the honest move. The gate is
    narrow: it fires only for the conflicting blocker (a THREADS_UNRESOLVED or
    CI_FAILING branch that merely happens to be behind is still owned, clearable
    work → salvage), and only when *commits_behind* is a known count — a ``None``
    behind-count (unavailable measurement) preserves :attr:`SALVAGE_AND_LAND`,
    failing safe toward salvage so the oracle never disposes on a guess.

    **Base-advance re-entry (099).** Distance behind alone no longer authorizes
    disposal. *same_base_failures* — the count of trailing ``partial`` laps that
    kept the same PR head against an unchanged ``origin/main`` — gates the
    behind-budget arm through :func:`_base_stall_permits_disengage`: omitted
    (:data:`_UNSET`, the flag-off path) reproduces today's distance-behind
    disengage byte-for-byte; an explicit ``None`` fails safe to
    :attr:`SALVAGE_AND_LAND` (never dispose on an unmeasurable streak); an ``int``
    disposes only once it reaches :data:`_STALLED_REMEDIATION_LAP_CAP`. A moved
    base is a new race, not grounds to give up.

    **Owed-act hold (proposal 137).** *code_owed* is the caller's reading of
    whether the PR is ``CODE_OWED`` — a blocker the loop clears by writing code
    — taken from the single :func:`~maestro.pr_owed_act.classify_pr_owed_act`
    partition. It arrives as a plain boolean rather than an ``OwedAct`` because
    this module is a leaf that ``pr_owed_act`` transitively imports; passing the
    enum would form a cycle. When it is ``True`` and the measured
    *commits_behind* is within :data:`RECONCILE_COMMITS_BEHIND_BUDGET`, the
    oracle returns :attr:`Salvage.SALVAGE_AND_LAND` ahead of the disposable arm:
    disengagement is not offered while the fix is owned and still cheap. It sits
    *after* the settled short-circuit, which must stay first — a merged or
    closed PR owes no code. Omitted (:data:`_UNSET`, the flag-off path) and an
    unmeasurable behind-count both reproduce today's table exactly; past the
    budget nothing changes, so a genuinely uncurable PR can still be closed.

    Reads :attr:`PRReadiness.settled` / :attr:`ready_and_waiting` /
    :attr:`branch_conflicting` off the injected fact plus the *disposable*
    boolean, *commits_behind*, *same_base_failures* and *code_owed*; does no
    I/O. Pure, deterministic, zero SDK cost.
    """
    if readiness.settled:
        return Salvage.DISENGAGE_PERMITTED
    if (
        code_owed is True
        and commits_behind is not None
        and commits_behind <= RECONCILE_COMMITS_BEHIND_BUDGET
    ):
        return Salvage.SALVAGE_AND_LAND
    if not readiness.ready_and_waiting and not disposable:
        if (
            readiness.branch_conflicting
            and commits_behind is not None
            and commits_behind > RECONCILE_COMMITS_BEHIND_BUDGET
            and _base_stall_permits_disengage(same_base_failures)
        ):
            return Salvage.DISENGAGE_PERMITTED
        return Salvage.SALVAGE_AND_LAND
    return Salvage.DISENGAGE_PERMITTED


# --- Restatement-threshold copy-pin — the leaf-preserving value dependency ---
#
# :func:`classify_pr_disposability` defaults its Jaccard *threshold* argument to
# this value. Its canonical home is :data:`maestro.consciousness.RESTATEMENT_THRESHOLD`,
# but importing it (or ``consciousness`` itself) at module-load time would drag that
# module's dependency tree (sdk, prompts, …) into this leaf base and form an import
# cycle — the same reasoning as the ``MERGE_BLOCK_*`` copies above. So the value is
# copy-pinned here and the two homes are held equal by an agreement test in
# ``tests/test_pr_facts.py``; the one real runtime call to
# :func:`~maestro.consciousness.detect_restatement` is a call-time deferred import
# inside the function body, never a module-load-time import.
RESTATEMENT_THRESHOLD: float = 0.5


def classify_pr_disposability(
    plan_text: str,
    sibling_plan_texts: Sequence[str],
    *,
    superseded_by_merged: bool | None,
    has_substantive_diff: bool | None,
    threshold: float = RESTATEMENT_THRESHOLD,
) -> bool:
    """Return ``True`` only when an open own PR's work is genuinely disposable.

    Disposable iff any of:

    * *plan_text* near-restates a sibling —
      ``detect_restatement(plan_text, list(sibling_plan_texts), threshold)`` finds
      a match. The sibling corpus is passed in — its caller now spans the *live*
      plans (``pending_review``/``approved``/``implemented``), so an ``approved``
      plan that has not yet opened a PR is compared too, not only the
      ``implemented`` open-PR backing plans that
      :func:`~maestro.memory.is_plan_restatement`'s callers — which feed it a
      ``pending_review``-only corpus — cannot see.
    * *superseded_by_merged* is ``True`` — a merged sibling already carries this
      work.
    * *has_substantive_diff* is ``False`` — the branch has no real change to land.

    The two I/O-derived signals are ``bool | None``: an unknown (``None``)
    contributes nothing toward disposability, so an unavailable detector fails
    toward *salvage* — the oracle never abandons work on a guess. Accepts
    pre-computed inputs and calls no host, so it does no I/O and stays
    deterministic under test. Pure, zero SDK cost.
    """
    # Call-time deferred import: keeps ``consciousness`` (and its sdk/prompts
    # dependency tree) off this leaf module's load path, so no import cycle forms.
    # The default ``threshold`` above is the copy-pinned :data:`RESTATEMENT_THRESHOLD`.
    from maestro.consciousness import detect_restatement

    if detect_restatement(plan_text, list(sibling_plan_texts), threshold) is not None:
        return True
    if superseded_by_merged is True:
        return True
    return has_substantive_diff is False
