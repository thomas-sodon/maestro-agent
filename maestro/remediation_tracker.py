"""Per-PR remediation repetition gate.

Tracks consecutive no-op remediation outcomes per PR and gates re-routing
when the last N outcomes are all no-ops. Resets on new branch commits
(detected by the routing method when a PR appears in conflict/quality-gate
routed lists, which require the branch state to have changed via main-merge).

Experimental — revert if <10% of re-routings prevented over 20 dispatches.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Literal,
    NamedTuple,
    Protocol,
    runtime_checkable,
)

from maestro.deed_ledger import DeedLedgerBackend, DeedType
from maestro.fileutil import atomic_write, file_lock
from maestro.messaging import Message, MessageStore
from maestro.narration_cues import (
    CAUSED_LANDING_PHRASES as _CAUSED_LANDING_PHRASES,
)
from maestro.narration_cues import (
    CONFIRMED_LANDING_PHRASES as _CONFIRMED_LANDING_PHRASES,
)
from maestro.narration_cues import (
    NOOP_ADMISSION_PHRASES as _NOOP_ADMISSION_PHRASES,
)
from maestro.pr_facts import (
    CI_FAILING,
    CONFLICTING,
    GREEN,
    LIVE_STATUSES,
    THREADS_UNRESOLVED,
    greenness_delta,
)
from maestro.pr_remediation_state import Marker, PrRemediationState

if TYPE_CHECKING:
    from maestro.audit_checks import (
        GateActivationBackend,
    )
    from maestro.audit_checks_settlement import (
        SettledVerdictsBackend,
    )
    from maestro.config import Config
    from maestro.memory import Memory, TaskRecord
    from maestro.pr_facts import MergeTruth

logger = logging.getLogger(__name__)

# Deterministic substring patterns that indicate a remediation task
# completed without making meaningful changes. Case-insensitive matching.
#
# ``remediation skipped`` matches the zero-cost ``[preflight]`` skip finding
# emitted when the PR branch already passes quality gates against the merged
# tree. That outcome is recorded as ``success``, but it produced no branch
# change — so the repetition gate must count it as a no-op, otherwise a PR
# that keeps preflight-skipping without ever merging reads as progress and
# loops forever (closure-blind accounting made mechanical).
NOOP_INDICATORS: tuple[str, ...] = (
    "already pass",
    "no changes needed",
    "required no remediation",
    "not reproducible",
    "required no code changes",
    # Keyword-bearing "already resolved" lives in ``narration_cues`` so the
    # record-time scrub protects it (#2970).
    *_NOOP_ADMISSION_PHRASES,
    "remediation skipped",
)

_DATA_FILE = "remediation_tracker.json"

# Schema version stamped into ``remediation_tracker.json``. Bumped to 2 when
# merge-block skips were split out of the ``should_reroute`` outcome list
# (#2796). A file whose stored ``schema_version`` is below this triggers the
# one-time merge-block reconciliation on load (see
# :meth:`RemediationTracker._reconcile_merge_block_history`), then the bump
# persists so it never re-runs. A wrapper/flat file lacking ``schema_version``
# is a pre-#2796 file and reads as :data:`_TRACKER_LEGACY_VERSION`; a missing or
# unreadable file reads as ``0`` and skips the migration (a fresh tracker has no
# conflated history to reconcile).
_TRACKER_SCHEMA_VERSION = 2
_TRACKER_LEGACY_VERSION = 1

# Trailing ``task_history.jsonl`` records the one-time #2796 reconciliation
# scans to count each PR's merge-block-origin no-op laps. Bounded so the
# migration never walks an unbounded history, yet generously sized so a stuck
# PR's recent merge-block laps are all in view.
_MERGE_BLOCK_RECONCILE_CYCLES = 200

# Persists ``{str(pr_number): last_passing_head_sha}`` so a PR confirmed
# passing the preflight gate at a given head SHA is not re-routed into
# ``pending_pr_requests`` every tend cycle (#2348). A new head SHA (e.g. a
# main-merge or remediation commit pushed by tend) bypasses the skip, since
# the merged tree changed and must be re-verified.
_PREFLIGHT_PASS_FILE = "preflight_passes.json"


def _preflight_passes_path(data_dir: Path) -> Path:
    """Return the path to the preflight-pass ledger under *data_dir*."""
    return data_dir / _PREFLIGHT_PASS_FILE


def _load_preflight_passes(path: Path) -> dict[str, str]:
    """Load the ``{pr_number: head_sha}`` ledger, tolerant of corruption."""
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                k: v
                for k, v in parsed.items()
                if isinstance(k, str) and isinstance(v, str)
            }
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def record_preflight_pass(
    pr_number: int, head_sha: str, data_dir: Path,
) -> None:
    """Record that *pr_number* passed the preflight gate at *head_sha*.

    Overwrites any prior recorded SHA for the PR — only the most recent
    confirmed-passing head matters for the routing skip decision. The
    read-modify-write is guarded by :func:`file_lock` so concurrent tend /
    task phases cannot interleave a lost update.
    """
    path = _preflight_passes_path(data_dir)
    with file_lock(path):
        data = _load_preflight_passes(path)
        data[str(pr_number)] = head_sha
        atomic_write(path, json.dumps(data))


def should_skip_routing(
    pr_number: int, head_sha: str, data_dir: Path,
) -> bool:
    """Return True when *pr_number*'s *head_sha* already passed preflight.

    Skips re-routing only when the PR's current head SHA exactly matches the
    last recorded passing SHA. An empty *head_sha* (unresolvable branch) never
    skips, so an inability to resolve the head can never suppress genuine
    remediation.
    """
    if not head_sha:
        return False
    path = _preflight_passes_path(data_dir)
    data = _load_preflight_passes(path)
    return data.get(str(pr_number)) == head_sha


@dataclass(frozen=True)
class PreflightPassRecord:
    """The full on-disk preflight-pass row (proposal 106).

    Carries the two columns the ``{pr_number: head_sha}`` ledger persists so the
    SQLite store can round-trip a row. The seed source
    :func:`load_all_preflight_passes` returns these. Mirrors
    :class:`FetchTruthRecord`, but the ledger is a per-PR *keyed replace* (only
    the latest passing head matters), not an append-only marker.
    """

    pr_number: int
    head_sha: str


def load_all_preflight_passes(data_dir: Path) -> list[PreflightPassRecord]:
    """Return every preflight-pass row — the SQLite seed source.

    Reads the ``{pr_number: head_sha}`` JSON via :func:`_load_preflight_passes`,
    sharing its corruption tolerance (non-``str`` keys/values are already
    dropped there, which also rejects the JSON-impossible ``bool`` key). A key
    that is not a base-ten integer is skipped as hand-edited or corrupt, since
    :func:`record_preflight_pass` only ever writes ``str(pr_number)`` keys. Rows
    are sorted by ``pr_number`` for a deterministic seed order — immaterial to
    the keyed-replace table, but it keeps the seed reproducible. It has a real
    call site (the :class:`~maestro.state_store.SqlitePreflightPassStore`
    one-time seed), so it does not trip the self-review uncalled-symbol gate.
    Read-only, zero SDK cost.
    """
    data = _load_preflight_passes(_preflight_passes_path(data_dir))
    records: list[PreflightPassRecord] = []
    for key, head_sha in data.items():
        try:
            pr_number = int(key)
        except ValueError:
            continue
        records.append(
            PreflightPassRecord(pr_number=pr_number, head_sha=head_sha),
        )
    records.sort(key=lambda r: r.pr_number)
    return records


@runtime_checkable
class PreflightPassBackend(Protocol):
    """The preflight-pass ledger persistence surface (proposal 106).

    One writer (the pre-remediation preflight in the tend loop) and one reader
    (the preflight-pass SHA dequeue gate) go through this seam, so the ledger can
    live in ``preflight_passes.json`` or in ``state.db`` behind ``[paths]
    sqlite_state_enabled`` with no call-site branching. Unlike the append-only
    marker ledgers this one is a per-PR *keyed replace* (only the latest passing
    head matters). Mirrors the :class:`FetchTruthBackend` idiom;
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    agent's backend-selection tests.
    """

    def record(self, pr_number: int, head_sha: str) -> None:
        """Record *pr_number* as passing preflight at *head_sha* (upsert)."""
        ...

    def should_skip(self, pr_number: int, head_sha: str) -> bool:
        """Return whether *pr_number*'s *head_sha* already passed preflight."""
        ...


class JsonPreflightPassLedger:
    """JSON-backed :class:`PreflightPassBackend` — the default, flag-off store.

    Binds a ``data_dir`` and delegates ``record`` / ``should_skip`` to the
    existing module-level :func:`record_preflight_pass` /
    :func:`should_skip_routing` — which stay the JSON read/write core, so the
    existing direct-call tests and the default path keep one code path. Mirrors
    :class:`JsonlFetchTruthLedger`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def record(self, pr_number: int, head_sha: str) -> None:
        record_preflight_pass(pr_number, head_sha, self._data_dir)

    def should_skip(self, pr_number: int, head_sha: str) -> bool:
        return should_skip_routing(pr_number, head_sha, self._data_dir)


# Deterministic merge-blocker reason codes returned by
# :func:`classify_merge_block`. A serviced-but-unmerged PR carries exactly one
# of these so an escalation names the real gate between branch and merge.
MERGE_BLOCK_BEHIND_MAIN = "BEHIND_MAIN"
MERGE_BLOCK_CONFLICTING = "CONFLICTING"
MERGE_BLOCK_CI_PENDING = "CI_PENDING"
MERGE_BLOCK_CI_FAILING = "CI_FAILING"
MERGE_BLOCK_AWAITING_APPROVAL = "AWAITING_APPROVAL"
MERGE_BLOCK_NONE = "NONE"
# Emitted only by ``MaestroAgent._probe_pr_merge_block`` (not
# :func:`classify_merge_block`): the host reports no blocker, but the resolution
# has not reached ``origin/<branch>`` yet — local-only commits or a missing
# origin ref. run()/Tend PR delivers it, so it is not a host-side gate
# requiring manual action; it names the real reason an otherwise-clean PR is
# still unlanded (#2457).
MERGE_BLOCK_AWAITING_PUSH = "AWAITING_PUSH"
# Not emitted by :func:`classify_merge_block` (which reads only merge-state/CI):
# an own PR whose sole remaining blocker is unresolved AI review threads. This
# is *owned*, code-clearable work — answer or refute the threads — never an
# external gate someone else must clear, so an approval wait or a clean merge
# state with an open thread names its real blocker as this, not the external
# code beneath it (#2907).
MERGE_BLOCK_THREADS_UNRESOLVED = "THREADS_UNRESOLVED"
# The three finer labels ``maestro.pr_facts.classify_merge_blocker_label`` splits
# the flat ``CI_PENDING`` rollup costume into, so a recorded ``merge blocker:``
# names the real gate: a running-CI wait to hold on (``CI_RUNNING``), a review
# wait handed to Thomas (``AWAITING_REVIEW`` — a review-decision-grounded sibling
# of :data:`MERGE_BLOCK_AWAITING_APPROVAL`), or a concrete CI failure to fix
# (:data:`MERGE_BLOCK_CI_FAILING`, reused). These two are canonical here; the
# leaf ``pr_facts`` module copies them (pinned by an agreement test) because it
# cannot import this module back without a cycle.
MERGE_BLOCK_CI_RUNNING = "CI_RUNNING"
MERGE_BLOCK_AWAITING_REVIEW = "AWAITING_REVIEW"


def classify_noop(result_summary: str) -> bool:
    """Return True when *result_summary* indicates a no-op remediation.

    Matches case-insensitively against :data:`NOOP_INDICATORS`.
    """
    if not result_summary:
        return False
    lower = result_summary.lower()
    return any(indicator in lower for indicator in NOOP_INDICATORS)


def classify_merge_block(
    commits_behind: int,
    check_conclusion: str,
    mergeable_state: str,
) -> str:
    """Classify why a serviced PR has not merged, from already-fetched state.

    Deterministic, zero SDK cost. Precedence (first match wins):

    1. :data:`MERGE_BLOCK_CONFLICTING` — the branch has merge conflicts with
       its base (``mergeable_state`` is ``DIRTY``); the conflict must be
       resolved before any CI result is mergeable, so it outranks CI state.
       It also outranks behind-ness: catching up (merging ``origin/main``) is
       exactly what *produces* the conflict, so a behind-AND-dirty branch must
       route to LLM conflict resolution, not the mechanical catch-up path (the
       merge-block gate skips LLM remediation for ``BEHIND_MAIN``) (#2521).
    2. :data:`MERGE_BLOCK_BEHIND_MAIN` — the branch trails ``origin/main``
       (``commits_behind`` > 0 or ``mergeable_state`` is ``BEHIND``) without a
       conflict; it must catch up before CI on the merged tree is meaningful.
    3. :data:`MERGE_BLOCK_CI_FAILING` — the status-check rollup concluded
       ``failure``.
    4. :data:`MERGE_BLOCK_CI_PENDING` — the rollup has not concluded
       (empty / ``pending``).
    5. :data:`MERGE_BLOCK_AWAITING_APPROVAL` — branch is current and green but
       review is outstanding (``mergeable_state`` is ``BLOCKED``).
    6. :data:`MERGE_BLOCK_NONE` — no deterministic blocker detected.
    """
    conclusion = (check_conclusion or "").strip().lower()
    state = (mergeable_state or "").strip().upper()
    if state == "DIRTY":
        return MERGE_BLOCK_CONFLICTING
    if commits_behind > 0 or state == "BEHIND":
        return MERGE_BLOCK_BEHIND_MAIN
    if conclusion == "failure":
        return MERGE_BLOCK_CI_FAILING
    if conclusion in ("", "pending"):
        return MERGE_BLOCK_CI_PENDING
    if state == "BLOCKED":
        return MERGE_BLOCK_AWAITING_APPROVAL
    return MERGE_BLOCK_NONE


def refine_merge_block_reason(
    reason: str,
    *,
    mergeable: str,
    check_conclusion: str = "",
    unpushed_or_missing: bool = False,
) -> str:
    """Refine a :func:`classify_merge_block` verdict with signals it never reads.

    Deterministic, zero SDK cost; shared by the merge-block probe and the
    readiness observer. The observer omits *check_conclusion* so its
    ``branch_behind`` stays a git fact — rule 2 is a routing decision only,
    and applying it there would read a still-behind red PR as "behind
    resolved" lap progress. First match wins:

    1. ``mergeable == "CONFLICTING"`` on a ``NONE`` / ``BEHIND_MAIN`` verdict →
       :data:`MERGE_BLOCK_CONFLICTING`. The host field can report a conflict
       before ``merge_state`` reads ``DIRTY``; catching up only reproduces the
       conflict, so it needs LLM resolution, not a Tend deferral (#2521).
    2. ``BEHIND_MAIN`` with a head CI conclusion of ``failure`` →
       :data:`MERGE_BLOCK_CI_FAILING`. Tend PR refuses to merge ``origin/main``
       into a red head (it routes a CI-failure remediation instead), so the
       catch-up premise behind ``BEHIND_MAIN``'s precedence is false; leaving
       it would let the merge-block gate evict the very remediation Tend
       routed (#4096). Skipped when *unpushed_or_missing*: Tend's red-head
       route yields to a committed-but-unpushed fix and pushes it, so the
       ``BEHIND_MAIN`` deferral stays correct.
    3. A ``NONE`` verdict whose resolution is not on ``origin/<branch>``
       (*unpushed_or_missing*) → :data:`MERGE_BLOCK_AWAITING_PUSH`, so a
       genuinely-unlanded PR never escalates as "Merge blocker: NONE" (#2457).
    """
    if mergeable == "CONFLICTING" and reason in (
        MERGE_BLOCK_NONE,
        MERGE_BLOCK_BEHIND_MAIN,
    ):
        return MERGE_BLOCK_CONFLICTING
    if (
        reason == MERGE_BLOCK_BEHIND_MAIN
        and not unpushed_or_missing
        and (check_conclusion or "").strip().lower() == "failure"
    ):
        return MERGE_BLOCK_CI_FAILING
    if reason == MERGE_BLOCK_NONE and unpushed_or_missing:
        return MERGE_BLOCK_AWAITING_PUSH
    return reason


def apply_landing_model(
    reason: str,
    *,
    tend_catch_up: bool,
    cleanly_mergeable: bool,
    check_conclusion: str,
    merge_state_status: str,
) -> str:
    """Resolve what a :data:`MERGE_BLOCK_BEHIND_MAIN` verdict means. Pure.

    The single owner of the "is behind-``main`` a blocker?" decision (#4307).
    :func:`classify_merge_block` ranks behind-ness as a blocker on the premise
    that Tend PR merges ``origin/main`` into the branch mechanically. That holds
    only while ``[loop] tend_main_merge_enabled`` (*tend_catch_up*) is on; under
    proposal 128's merge-queue model the queue tests each PR against the tree it
    lands into, so trailing ``main`` is neither a landing precondition nor
    anything the loop clears.

    With *tend_catch_up* the verdict is returned unchanged. Otherwise a
    ``BEHIND_MAIN`` verdict becomes :data:`MERGE_BLOCK_CONFLICTING` when the
    branch no longer merges cleanly (*cleanly_mergeable* False), else it is
    re-classified with behind-ness removed — so the CI and approval rungs
    behind it (``CI_FAILING`` / ``CI_PENDING`` / ``AWAITING_APPROVAL`` /
    ``NONE``) still speak. Every other code passes through untouched.
    """
    if tend_catch_up or reason != MERGE_BLOCK_BEHIND_MAIN:
        return reason
    if not cleanly_mergeable:
        return MERGE_BLOCK_CONFLICTING
    state = (merge_state_status or "").strip().upper()
    if state == "BEHIND":
        state = ""
    return classify_merge_block(0, check_conclusion, state)


# Ownership of a merge blocker — whether resolving it is *mine* (reconcile or
# close the PR myself) or *external* (someone else / Tend must act). Returned by
# :func:`classify_blocker_ownership`.
BLOCKER_OWNERSHIP_OWNED = "owned"
BLOCKER_OWNERSHIP_EXTERNAL = "external"

# Merge-block reason codes whose resolution is mine to drive: a conflicting /
# dirty branch is reconciled (merge ``origin/main``, fix conflicts, push) or
# closed by me — Tend cannot mechanically clear it. ``DIRTY`` is the raw
# ``mergeable_state`` GitHub reports for a conflicting branch and maps to
# :data:`MERGE_BLOCK_CONFLICTING`; both are treated as owned so a caller reading
# either the classified code or the raw host state agrees.
# :data:`MERGE_BLOCK_THREADS_UNRESOLVED` joins them: an un-answered AI review
# thread on my own PR is owned, code-clearable work, never an external gate.
# Public (proposal 098): :func:`classify_blocker_ownership` is the single source
# of the owned/external partition, so downstream consumers read this set rather
# than re-listing reason codes.
OWNED_BLOCKERS = frozenset(
    {MERGE_BLOCK_CONFLICTING, "DIRTY", MERGE_BLOCK_THREADS_UNRESOLVED},
)

# The single reason code the ``own_push`` rollout flag reclassifies from
# *external* to *owned* (proposal 098): :data:`MERGE_BLOCK_AWAITING_PUSH` — a
# conflict resolution the agent has committed to its own ``maestro/evolve/*``
# branch but not yet pushed. Now that the agent can push its own branch, an
# unpushed on-branch resolution is an owned next act, not a push to defer to
# Tend. It stays out of :data:`OWNED_BLOCKERS` so the default (flag-off) path is
# byte-identical to today; only ``own_push=True`` folds it into the owned class.
OWNED_PUSH_BLOCKER = MERGE_BLOCK_AWAITING_PUSH

# Merge-block reason codes that a live unresolved-thread count promotes to
# :data:`MERGE_BLOCK_THREADS_UNRESOLVED` in :func:`classify_owned_blocker`: an
# approval wait (:data:`MERGE_BLOCK_AWAITING_APPROVAL`) or a clean merge state
# (:data:`MERGE_BLOCK_NONE`) is external/none *only* while no review thread is
# open — an un-answered AI thread is owned work that outranks it. Conflict /
# behind / CI reasons name a different, still-blocking gate and are left as-is.
_THREAD_PROMOTABLE_BLOCKERS = frozenset(
    {MERGE_BLOCK_AWAITING_APPROVAL, MERGE_BLOCK_NONE},
)


def classify_blocker_ownership(blocker: str, *, own_push: bool = False) -> str:
    """Classify a merge blocker as ``'owned'`` or ``'external'``.

    This is the single, flag-aware definition of the owned/external partition:
    every consumer reads it (or the exported :data:`OWNED_BLOCKERS` /
    :data:`OWNED_PUSH_BLOCKER` constants) rather than re-encoding the taxonomy.

    Returns :data:`BLOCKER_OWNERSHIP_OWNED` for the blockers in
    :data:`OWNED_BLOCKERS` — :data:`MERGE_BLOCK_CONFLICTING` and the raw
    ``DIRTY`` host state it derives from (a conflicting branch must be
    reconciled — merge ``origin/main``, fix conflicts, push — or closed by me;
    Tend cannot clear it mechanically), plus
    :data:`MERGE_BLOCK_THREADS_UNRESOLVED` (an un-answered AI review thread on my
    own PR is answered/refuted by me).

    When *own_push* is true, :data:`OWNED_PUSH_BLOCKER`
    (:data:`MERGE_BLOCK_AWAITING_PUSH`) also classifies *owned* (proposal 098):
    a conflict resolution committed to my own ``maestro/evolve/*`` branch but not
    yet pushed is an owned next act now that the agent can push its own branch,
    not a push to defer to Tend. The default ``own_push=False`` leaves
    ``AWAITING_PUSH`` *external*, so the flag-off path is byte-identical to the
    pre-098 classifier.

    Returns :data:`BLOCKER_OWNERSHIP_EXTERNAL` for every other blocker —
    :data:`MERGE_BLOCK_AWAITING_APPROVAL` (a human must approve),
    :data:`MERGE_BLOCK_AWAITING_PUSH` (unless *own_push*) /
    :data:`MERGE_BLOCK_BEHIND_MAIN` (Tend's mechanical catch-up / push delivers
    them), CI states, and any unrecognised or empty blocker. *External* is the
    safe default: it preserves the prior routing (re-admission as a remediation
    task), so an unknown or unparsed blocker can never be wrongly diverted out of
    remediation.

    Casing and surrounding whitespace are normalised before matching, so a raw
    ``" dirty "`` or ``"conflicting"`` classifies the same as the canonical
    upper-case code. Pure, deterministic, zero SDK cost.
    """
    normalised = (blocker or "").strip().upper()
    if normalised in OWNED_BLOCKERS:
        return BLOCKER_OWNERSHIP_OWNED
    if own_push and normalised == OWNED_PUSH_BLOCKER:
        return BLOCKER_OWNERSHIP_OWNED
    return BLOCKER_OWNERSHIP_EXTERNAL


#: Sentinel :func:`classify_host_pr_terminal` returns when the code host reports
#: a PR CLOSED without merging — the human's own terminal, un-landable by any
#: remediation lap. Distinct from the merged/landed settled arms: a
#: closed-unmerged PR is *settled* yet its branch never reached ``main``.
HOST_CLOSED_UNMERGED = "host_closed_unmerged"


def classify_host_pr_terminal(pr_state: MergeTruth | None) -> str | None:
    """Read an already-fetched merge-truth record for a human-closed terminal.

    Pure, zero-cost, no network: inspects the per-iteration
    :class:`~maestro.pr_facts.MergeTruth` a caller already observed through
    the :meth:`~maestro.agent.MaestroAgent.is_pr_settled` chokepoint and returns
    the :data:`HOST_CLOSED_UNMERGED` sentinel **only** when the host reports the
    PR CLOSED with its branch unmerged (``state == "CLOSED"`` and **not**
    ``branch_landed`` — the ``mergedAt``-null fork). A closed-unmerged PR is a
    terminal the human already chose: no remediation lap can land it, so reading
    that state and evicting is reality-grounding, not giving up on a live PR.

    Returns ``None`` — the fail-safe passthrough that leaves the PR remediable —
    for an OPEN PR (``state == "OPEN"``), a merged PR (``branch_landed`` /
    ``state == "MERGED"``, including the defensive CLOSED-and-merged shape where
    the host records a ``mergedAt``), and a missing/``None`` record. Never
    raises: a caller that could not observe a record passes ``None`` and the PR
    is left untouched. Deterministic; the branch a caller keys eviction on is a
    function of the record alone.
    """
    if pr_state is None:
        return None
    if pr_state.state == "CLOSED" and not pr_state.branch_landed:
        return HOST_CLOSED_UNMERGED
    return None


def classify_owned_blocker(reason: str, unresolved_threads: int) -> str:
    """Promote an approval/none *reason* to THREADS_UNRESOLVED when threads are open.

    Codifies "approval withheld (or a clean merge state) while my own PR still
    has unresolved AI review threads is *owned*, not external": when
    *unresolved_threads* is positive and *reason* is an otherwise-external
    :data:`MERGE_BLOCK_AWAITING_APPROVAL` or a clean :data:`MERGE_BLOCK_NONE`
    (the :data:`_THREAD_PROMOTABLE_BLOCKERS` set), returns
    :data:`MERGE_BLOCK_THREADS_UNRESOLVED` — the owned, code-clearable blocker
    an un-answered thread names. A conflict / behind / CI reason names a
    different, still-blocking gate and is returned unchanged, as is any reason
    when no thread is open.

    Casing and surrounding whitespace are normalised before matching, and an
    empty reason is read as :data:`MERGE_BLOCK_NONE`. Pure, deterministic, zero
    SDK cost.
    """
    normalised = (reason or "").strip().upper() or MERGE_BLOCK_NONE
    if unresolved_threads > 0 and normalised in _THREAD_PROMOTABLE_BLOCKERS:
        return MERGE_BLOCK_THREADS_UNRESOLVED
    return normalised


class TrackerState(NamedTuple):
    """The four per-PR maps :class:`RemediationTracker` persists, plus its version.

    The whole-state unit that crosses the :class:`RemediationTrackerBackend`
    seam: ``outcomes`` (the consecutive no-op lists the re-route gate reasons
    over), ``items_remaining`` (the outstanding review-item baseline),
    ``skips`` (repetition-gate suppressions), ``merge_block_skips``
    (merge-block deferrals), and the stored ``schema_version`` that decides
    whether the one-time #2796 merge-block reconciliation still owes a run.

    Version follows :meth:`RemediationTracker._load`'s historical contract:
    ``0`` for a fresh tracker (no file / unreadable / not a mapping — nothing
    to reconcile), :data:`_TRACKER_LEGACY_VERSION` for a present payload
    lacking a valid integer ``schema_version``, else the stored value.
    """

    outcomes: dict[str, list[bool]]
    items_remaining: dict[str, int]
    skips: dict[str, int]
    merge_block_skips: dict[str, int]
    schema_version: int


def _parse_tracker_payload(raw: object) -> TrackerState:
    """Parse a decoded ``remediation_tracker.json`` payload into a state.

    The corrupt-tolerant parse core lifted verbatim from the historical
    :meth:`RemediationTracker._load` so both substrates share one reader.
    Accepts the current wrapper format (``{"outcomes": …, "items_remaining":
    …, "skips": …, "merge_block_skips": …, "schema_version": …}``) and the
    legacy flat format (PR number → outcome list) written before #2744 — a
    dict whose ``outcomes`` value is itself a dict is unambiguously the new
    form. A legacy file carries no items-remaining baseline, so the
    comment-progress comparison falls back to today's any-remaining
    classification until the first new lap records a count; it likewise
    carries no ``skips`` / ``merge_block_skips`` maps, so those start at zero.

    Anything that is not a mapping reads as a fresh tracker (empty maps,
    version ``0``); structurally-wrong values inside a map are dropped rather
    than raised, and ``bool`` is excluded from the integer maps since it
    subclasses ``int``.
    """
    if not isinstance(raw, dict):
        return TrackerState({}, {}, {}, {}, 0)
    if isinstance(raw.get("outcomes"), dict):
        outcomes_raw = raw["outcomes"]
        items_raw = raw.get("items_remaining")
        skips_raw = raw.get("skips")
        merge_block_raw = raw.get("merge_block_skips")
        version_raw = raw.get("schema_version")
    else:
        outcomes_raw = raw
        items_raw = None
        skips_raw = None
        merge_block_raw = None
        version_raw = None
    outcomes = {
        k: v for k, v in outcomes_raw.items()
        if isinstance(v, list) and all(isinstance(x, bool) for x in v)
    }
    items: dict[str, int] = {}
    if isinstance(items_raw, dict):
        # ``bool`` subclasses ``int`` — exclude it so a stray serialized
        # bool never poses as a count.
        items = {
            k: v for k, v in items_raw.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
    skips: dict[str, int] = {}
    if isinstance(skips_raw, dict):
        # Same ``bool``-excluding guard as the items baseline above.
        skips = {
            k: v for k, v in skips_raw.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
    merge_block_skips: dict[str, int] = {}
    if isinstance(merge_block_raw, dict):
        # Same ``bool``-excluding guard as the skips count above.
        merge_block_skips = {
            k: v for k, v in merge_block_raw.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
    # Any present file lacking a valid integer ``schema_version`` is a
    # pre-#2796 file (legacy flat or wrapper) eligible for the one-time
    # reconciliation; ``bool`` is excluded since it subclasses ``int``.
    version = (
        version_raw
        if isinstance(version_raw, int) and not isinstance(version_raw, bool)
        else _TRACKER_LEGACY_VERSION
    )
    return TrackerState(outcomes, items, skips, merge_block_skips, version)


def load_tracker_state(data_dir: Path) -> TrackerState:
    """Read ``remediation_tracker.json`` under *data_dir* into a state.

    The shared seed source: :class:`JsonRemediationTrackerStore` reads through
    it on the flag-off path, and
    :class:`~maestro.state_store.SqliteRemediationTrackerStore` reads through
    it once to seed ``state.db`` from the retained legacy sidecar. A missing or
    unparseable file is a fresh tracker (empty maps, version ``0``) — never a
    raise, matching the historical load.
    """
    try:
        raw = json.loads((data_dir / _DATA_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return TrackerState({}, {}, {}, {}, 0)
    return _parse_tracker_payload(raw)


@runtime_checkable
class RemediationTrackerBackend(Protocol):
    """The two persistence primitives :class:`RemediationTracker` needs.

    A whole-state :meth:`load` at construction and a whole-state
    :meth:`persist` after every mutation — the tracker's load-once,
    in-memory-cache lifecycle is unchanged, so unlike
    :class:`~maestro.pr_ledger.PRLedgerBackend` no per-operation
    read-modify-write primitive is needed (a single instance per agent owns
    the state, shared by reference with ``RemediationTrackerReconciler``).
    Only these two move behind the seam; the no-op threshold, skip accounting,
    and the one-time #2796 merge-block reconciliation all stay in the tracker.
    Satisfied by :class:`JsonRemediationTrackerStore` (``remediation_tracker.json``)
    and :class:`~maestro.state_store.SqliteRemediationTrackerStore`
    (``state.db`` behind ``[paths] sqlite_state_enabled``, proposal 106);
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    agent's backend-selection tests.
    """

    def load(self) -> TrackerState:
        """Return the persisted state (a fresh, version-``0`` state when absent)."""
        ...

    def persist(self, state: TrackerState) -> None:
        """Persist *state* whole, replacing whatever was stored before."""
        ...


class JsonRemediationTrackerStore:
    """Default :class:`RemediationTrackerBackend` — the ``remediation_tracker.json`` store.

    Keeps the historical persistence byte-for-byte: :meth:`load` delegates to
    the shared :func:`load_tracker_state` reader, and :meth:`persist` is the
    lifted :meth:`RemediationTracker._persist` body — one whole-file
    :func:`~maestro.fileutil.atomic_write` of the wrapper dict under
    :func:`~maestro.fileutil.file_lock`, so cross-process atomicity is
    unchanged. This is the flag-off store (proposal 106); the ``state.db``
    selection is
    :class:`~maestro.state_store.SqliteRemediationTrackerStore`. Mirrors
    :class:`JsonPreflightPassLedger`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._path = data_dir / _DATA_FILE

    def load(self) -> TrackerState:
        return load_tracker_state(self._data_dir)

    def persist(self, state: TrackerState) -> None:
        with file_lock(self._path):
            atomic_write(
                self._path,
                json.dumps(
                    {
                        "outcomes": state.outcomes,
                        "items_remaining": state.items_remaining,
                        "skips": state.skips,
                        "merge_block_skips": state.merge_block_skips,
                        "schema_version": state.schema_version,
                    },
                ),
            )


class RemediationTracker:
    """Tracks per-PR remediation outcomes and gates re-routing.

    Parameters
    ----------
    data_dir:
        Directory where ``remediation_tracker.json`` is persisted.
    threshold:
        Number of consecutive no-op outcomes required to block re-routing.
    enabled:
        Master switch; when False, :meth:`should_reroute` always returns True.
    backend:
        Persistence seam (proposal 106); defaults to the
        :class:`JsonRemediationTrackerStore` over *data_dir*, so the flag-off
        on-disk behaviour is unchanged. ``state.db`` is selected by passing
        :class:`~maestro.state_store.SqliteRemediationTrackerStore`.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        threshold: int = 2,
        enabled: bool = True,
        backend: RemediationTrackerBackend | None = None,
    ) -> None:
        # ``_path`` stays derived from *data_dir* on either substrate: the
        # one-time #2796 reconciliation reads ``task_history.jsonl`` from its
        # parent, which is the data dir regardless of where state lives.
        self._path = data_dir / _DATA_FILE
        self._backend: RemediationTrackerBackend = (
            backend if backend is not None
            else JsonRemediationTrackerStore(data_dir)
        )
        self._threshold = threshold
        self._enabled = enabled
        # ``_data`` holds the per-PR no-op outcome lists the gate reasons over;
        # ``_last_items`` holds the most recent outstanding review-item count per
        # PR, the baseline the comment-progress comparison in
        # ``_record_remediation_outcome`` reads to tell a terminal-reply-delivering
        # lap from a no-op one (#2744). Both persist together as one
        # :class:`TrackerState` through the injected backend.
        self._data: dict[str, list[bool]]
        self._last_items: dict[str, int]
        # ``_skips`` counts the Tend repetition-gate re-route suppressions per PR
        # (#2783): a suppressed re-route mints no task and stamps no outcome, so
        # it never reaches ``measure_unlanded_remediations`` — yet it is a lap
        # that landed nothing. The Tend lap-budget gate adds this count to the
        # stamped-lap count so an aggressively-skipped PR still reaches the
        # budget and is handed up. Reset alongside the no-op list on a
        # branch-state change.
        self._skips: dict[str, int]
        # ``_merge_block_skips`` counts the merge-block deferrals per PR (#2796).
        # A merge-block skip ("Tend PR clears this mechanically") is a deliberate
        # deferral, not a failed LLM remediation, so it must never enter the
        # ``should_reroute`` outcome list and exile a PR's still-unattempted
        # comment/overflow work. It is routed here instead — kept for observance,
        # ignored by the gate. Reset alongside the no-op list on a branch-state
        # change.
        self._merge_block_skips: dict[str, int]
        (
            self._data,
            self._last_items,
            self._skips,
            self._merge_block_skips,
            loaded_version,
        ) = self._load()
        # One-time #2796 reconciliation: a file written before the merge-block
        # split conflated merge-block no-ops into the ``should_reroute`` outcome
        # list, exiling PRs whose comment/overflow work was never attempted.
        # Re-derive the list without them, then stamp the new schema version so
        # the migration runs exactly once. A fresh tracker (version 0) has no
        # conflated history and is stamped on its first persist instead.
        if _TRACKER_LEGACY_VERSION <= loaded_version < _TRACKER_SCHEMA_VERSION:
            self._reconcile_merge_block_history()

    def _load(self) -> TrackerState:
        """Load persisted tracker state through the injected backend.

        The parse itself lives in the shared, corrupt-tolerant
        :func:`_parse_tracker_payload` (reached via
        :func:`load_tracker_state` on the JSON path), so both substrates
        return the same :class:`TrackerState` — including the stored schema
        version the one-time #2796 merge-block reconciliation keys off.
        """
        return self._backend.load()

    def _persist(self) -> None:
        """Persist current state whole, stamped with the current schema version.

        Stamping here (rather than in a backend) keeps the version-bump policy
        with the migration that owns it: the first persist after a pre-#2796
        load lands :data:`_TRACKER_SCHEMA_VERSION`, so the reconciliation
        never re-runs on either substrate.
        """
        self._backend.persist(
            TrackerState(
                outcomes=self._data,
                items_remaining=self._last_items,
                skips=self._skips,
                merge_block_skips=self._merge_block_skips,
                schema_version=_TRACKER_SCHEMA_VERSION,
            ),
        )

    def record_outcome(
        self,
        pr_number: int,
        *,
        noop: bool,
        items_remaining: int | None = None,
        merge_block: bool = False,
    ) -> None:
        """Record whether the latest remediation was a no-op.

        Appends to the per-PR outcome list and persists immediately. When
        ``items_remaining`` is given, it is stored as the PR's new outstanding
        review-item baseline so the next lap's comment-progress comparison
        (#2744) has a count to measure against; ``None`` leaves any existing
        baseline untouched.

        When ``merge_block`` is True the event is a merge-block skip — a
        deliberate "Tend PR clears this mechanically" deferral, not a failed LLM
        remediation — so it is routed to the separate
        :meth:`merge_block_skip_count` counter instead of the ``should_reroute``
        outcome list (#2796). The skip's ``partial`` :class:`TaskRecord` (written
        by the caller) still feeds ``measure_unlanded_remediations``, so the
        #2783 lap-budget standdown stays reachable; only the goal-blind re-route
        gate is spared the poisoning no-op. ``noop`` and ``items_remaining`` are
        ignored on this path — the merge-block nit was never read, let alone
        attempted, so it carries no review-progress signal.
        """
        key = str(pr_number)
        if merge_block:
            self._merge_block_skips[key] = self._merge_block_skips.get(key, 0) + 1
            self._persist()
            return
        outcomes = self._data.setdefault(key, [])
        outcomes.append(noop)
        if items_remaining is not None:
            self._last_items[key] = items_remaining
        self._persist()

    def last_items_remaining(self, pr_number: int) -> int | None:
        """Return the most recently recorded outstanding review-item count.

        ``None`` when no count has been recorded for the PR — the
        comment-progress comparison in ``_record_remediation_outcome`` then has
        no baseline and falls back to today's any-remaining no-op classification.
        """
        return self._last_items.get(str(pr_number))

    def should_reroute(self, pr_number: int) -> bool:
        """Return False when the last *threshold* outcomes are all no-ops.

        Always returns True when the gate is disabled, when there is no
        history, or when the consecutive no-op count is below threshold.
        """
        if not self._enabled:
            return True
        key = str(pr_number)
        outcomes = self._data.get(key)
        if not outcomes:
            return True
        if len(outcomes) < self._threshold:
            return True
        # Check if the last `threshold` outcomes are all no-ops
        return not all(outcomes[-self._threshold:])

    def noop_count(self, pr_number: int) -> int:
        """Return the count of trailing consecutive no-op outcomes for a PR.

        The number of laps that landed nothing in a row — the honest "stuck"
        depth a durable hand-up records as its ``stamp_count`` (#2783). Zero
        when the PR has no recorded outcomes or its most recent lap was
        productive.
        """
        outcomes = self._data.get(str(pr_number))
        if not outcomes:
            return 0
        count = 0
        for noop in reversed(outcomes):
            if not noop:
                break
            count += 1
        return count

    def record_skip(self, pr_number: int) -> int:
        """Record one Tend repetition-gate re-route suppression; return the count.

        A re-route the repetition gate suppresses mints no task and stamps no
        outcome, so :func:`measure_unlanded_remediations` never sees it — yet it
        is a genuine lap that landed nothing (#2783). This per-PR counter lets
        the Tend lap-budget gate add suppressed laps to the stamped-lap count so
        an aggressively-skipped PR still reaches the budget and is handed up.
        Reset on a branch-state change via :meth:`reset`, exactly like the no-op
        outcome list.
        """
        key = str(pr_number)
        self._skips[key] = self._skips.get(key, 0) + 1
        self._persist()
        return self._skips[key]

    def skip_count(self, pr_number: int) -> int:
        """Return the recorded repetition-gate skip count for *pr_number*.

        Zero when no skip has been recorded (or the PR's history was reset on a
        branch-state change).
        """
        return self._skips.get(str(pr_number), 0)

    def merge_block_skip_count(self, pr_number: int) -> int:
        """Return the recorded merge-block skip count for *pr_number* (#2796).

        Merge-block skips ("Tend PR clears this mechanically" deferrals) are
        recorded here rather than in the ``should_reroute`` outcome list, so they
        never exile a PR's still-unattempted comment/overflow work. Zero when
        none has been recorded (or the PR's history was reset on a branch-state
        change).
        """
        return self._merge_block_skips.get(str(pr_number), 0)

    def _purge_pr(self, pr_number: int) -> bool:
        """Remove *pr_number*'s keys from all four axes; persist only on change.

        The shared four-axis deletion behind both :meth:`reset` (a branch-state
        "earn the budget back" event) and :meth:`evict_settled` (a settlement
        "forget this PR" event, proposal 103). Drops the PR's no-op outcome list,
        outstanding review-item baseline, repetition-gate skip count, and
        merge-block skip count, persisting only when something was actually
        removed so a purge of an absent PR writes nothing. Returns whether any
        axis carried a key for the PR (the persist-only-on-change guard doubling
        as the "one ledger row, then silence" signal a settlement reconciler
        reads).
        """
        key = str(pr_number)
        changed = False
        if key in self._data:
            del self._data[key]
            changed = True
        if key in self._last_items:
            del self._last_items[key]
            changed = True
        if key in self._skips:
            del self._skips[key]
            changed = True
        if key in self._merge_block_skips:
            del self._merge_block_skips[key]
            changed = True
        if changed:
            self._persist()
        return changed

    def reset(self, pr_number: int) -> None:
        """Clear outcome history for a PR (e.g. on new branch commits).

        Drops the no-op outcome list, the outstanding review-item baseline, the
        repetition-gate skip count, and the merge-block skip count so a reset PR
        starts fresh on all four axes — a branch-state change earns the full
        budget back, including the suppressed-lap count the lap-budget gate reads
        (#2783) and the merge-block deferral count (#2796). Delegates the purge to
        :meth:`_purge_pr`; the removed-anything result is irrelevant to a reset
        (behaviour-preserving), so it is discarded.
        """
        self._purge_pr(pr_number)

    def evict_settled(self, pr_number: int) -> bool:
        """Forget a settled PR's remediation records; return whether any existed.

        The settlement "this PR is done — forget it" event (proposal 103): when a
        Maestro-owned PR merges or closes, its per-PR remediation records should
        no longer gate any reasoning. Shares :meth:`_purge_pr`'s four-axis
        deletion with :meth:`reset` but carries distinct meaning — ``reset`` earns
        the budget back on a branch-state change, ``evict_settled`` forgets a done
        PR — and returns the purge result so the on-settlement record reconciler
        can report whether it actually cleared a trace (``True``) or found nothing
        to clear (``False``, no persist). Mutates this shared long-lived instance
        directly, exactly as ``reset`` does, so the settlement clear cannot be
        clobbered by the next in-process persist.
        """
        return self._purge_pr(pr_number)

    def _reconcile_merge_block_history(self) -> None:
        """One-time #2796 reconciliation of pre-split conflated history.

        Re-derives each PR's ``should_reroute`` outcome list to exclude
        merge-block-origin no-ops — counted from the :data:`_MERGE_BLOCK_MARKER`
        signature in ``task_history.jsonl`` — then persists with the bumped
        schema version so it runs exactly once (gated by the version check in
        ``__init__``). Genuinely-attempted no-op history (a ``Remediate PR #N``
        lap whose summary lacks the marker) is preserved, so the #2744 guard
        stays intact. Always persists, even when nothing changed, so the version
        bump lands and the migration never re-runs.
        """
        tasks = _read_recent_task_tail(
            self._path.parent, _MERGE_BLOCK_RECONCILE_CYCLES,
        )
        self._data = _strip_merge_block_outcomes(self._data, tasks)
        self._persist()


# ---------------------------------------------------------------------------
# Unlanded-remediation detector — git-merge-truth ground truth
# ---------------------------------------------------------------------------
#
# The stamp-based recurrence detectors in ``audit_checks`` reason only from
# task outcomes and point-in-time merge-block guesses. None consult git, the
# only authoritative record of whether a PR's commit actually reached ``main``.
# This detector adds that missing ground-truth sensor: a remediation stamped
# ``success``/``partial`` whose PR git-merge-truth shows is *not* merged into
# ``main`` is *unlanded* — true progress was claimed, but nothing landed. It
# consults the same ``_git_pr_merged_default`` oracle the PR-keyed audit rules
# use, so both sensors agree on what "landed" means. A prior
# version cross-referenced ``version_archive.jsonl`` instead of git; that
# false-flagged PRs whose commit reached ``main`` but whose ``VersionRecord``
# title/number never matched (#2504). Measurement-only: it surfaces the red
# number, it does not act on it.

# Filename under ``data_dir`` consulted by the detector.
_TASK_HISTORY_FILE = "task_history.jsonl"

# Matches the unified remediation-goal header rendered by
# ``agent._compose_pr_remediation_prompt`` — ``Remediate PR #<n>: <title>``.
# Group 1 is the PR number (the git-merge-truth key); group 2 is the PR title,
# retained for the goal-shape match but no longer used for landing evidence.
_REMEDIATION_GOAL_PATTERN = re.compile(r"Remediate PR #(\d+):\s*(.*)")

# Remediation outcomes that count as a "stamp" — both represent a remediation
# the agent recorded as complete enough to claim progress.
_REMEDIATION_STAMP_OUTCOMES = frozenset({"success", "partial"})

# Literal signatures of the agent's own documented costume — every ``partial``
# remediation lap whose resolution exists only on the branch while *something
# other than a fresh LLM lap* is what would land it. The blocker label shifts
# across laps (CONFLICTING resolves on-branch → ``awaiting tend push``;
# AWAITING_PUSH / BEHIND_MAIN are mechanically skipped → ``[merge-block]``; an
# owned branch whose push failed or was oracle-refused →
# ``[owned-push-available]``), but every one is the same non-event: nothing
# landed. Counting **all** of these signatures — not just the on-branch wording
# — makes the costume-signature count verdict-agnostic, so a shifting blocker
# label cannot reset or dodge it.
#
# ``OWNED_PUSH_AVAILABLE`` is the *owned* member of the set (proposal 098). An
# AWAITING_PUSH lineage is *owned*, so it no longer accrues the two Tend-clearable
# markers at all — ``gate_merge_block`` declines to skip (so no ``[merge-block]``
# stamp) and the preamble steers the agent off the ``awaiting tend push`` wording
# — so a lineage whose push keeps failing would otherwise burn full SDK laps to
# the escalation-bridge lap budget instead of latching at the much cheaper costume
# threshold. Its own failure stamp is what keeps the cheap brake armed; a lap that
# actually pushed stamps ``LANDED`` / ``DELIVERED_AWAITING_MERGE`` instead and is
# correctly not counted.
#
# Substrings are matched case-insensitively (every literal is already lowercase)
# so the dequeue-face gate detects the futile lap by its own confident wording
# and stops the wheel rather than re-lapping it.
_BRANCH_RESOLVED_MARKER = Marker.AWAITING_TEND_PUSH
_MERGE_BLOCK_MARKER = Marker.MERGE_BLOCK
_OWNED_PUSH_MARKER = Marker.OWNED_PUSH_AVAILABLE
# ``PUSH_UNCONFIRMED`` is the sibling non-delivery of ``OWNED_PUSH_AVAILABLE``
# (proposal 099): the push exited 0 but a same-turn remote-head read showed
# origin never advanced, so the lap is just as undelivered on-branch as a
# failed/never-attempted push. Counting it keeps the cheap costume-signature
# brake armed on a lineage whose push keeps no-op-ing, exactly as the owned
# member does — otherwise such a lineage burns full laps to the escalation
# budget instead of latching here.
_PUSH_UNCONFIRMED_MARKER = Marker.PUSH_UNCONFIRMED
_UNDELIVERED_PARTIAL_MARKERS = frozenset({
    _BRANCH_RESOLVED_MARKER,
    _MERGE_BLOCK_MARKER,
    _OWNED_PUSH_MARKER,
    _PUSH_UNCONFIRMED_MARKER,
})

# Prefix of the costume-signature gate's own zero-cost skip stamp
# (``gate_branch_resolved``). Such a stamp is a ``partial`` ``Remediate PR
# #<n>`` record, so without this exclusion it would satisfy the counting
# predicate and the latch would re-arm itself on every firing — never decaying
# out of the trailing window (#2595). Matched case-insensitively against the
# lowercased summary, consistent with the marker-matching convention.
_BRANCH_RESOLVED_SKIP_PREFIX = Marker.BRANCH_RESOLVED


def count_branch_resolved_partials(
    tasks: list[dict[str, object]],
    pr_number: int,
    *,
    is_delivered: Callable[[str], bool] | None = None,
) -> int:
    """Count ``partial`` undelivered remediation laps for *pr_number*.

    Scans *tasks* (task-history records) for entries whose ``goal`` (or ``title``
    fallback) matches ``Remediate PR #<n>: <title>`` with ``n == pr_number``,
    whose ``outcome`` is exactly ``partial``, and whose ``result_summary``
    carries any signature in :data:`_UNDELIVERED_PARTIAL_MARKERS`
    (case-insensitive) — the on-branch ``awaiting tend push`` wording, the
    mechanical ``[merge-block]`` skip, *or* the owned ``[owned-push-available]``
    stamp of a lap whose push failed. Each such record is one wheel-lap that
    landed nothing because some blocker (CONFLICTING resolved on-branch,
    AWAITING_PUSH, BEHIND_MAIN, or a refused/failed owned push) kept the PR off
    ``main``.

    Counting every undelivered signature — not only the on-branch wording — is
    what makes the count **verdict-agnostic**: the merge-blocker label shifts
    between laps (and the merge-block gate intercepts the AWAITING_PUSH /
    BEHIND_MAIN ones before this gate runs), so a counter keyed to a single label
    under-counts and lets the wheel keep turning. Accumulating by ``#NNNN``
    lineage across labels closes that dodge. Including the owned
    ``[owned-push-available]`` stamp keeps the brake armed now that an
    AWAITING_PUSH lineage is owned and stops accruing the two Tend-clearable
    markers entirely (proposal 098).

    Pure, read-only, zero SDK cost. Returns ``0`` when no record matches. Used by
    the dequeue-face costume-signature gate to zero-cost-skip and escalate a PR
    that keeps resolving-on-branch without ever merging, rather than re-lapping
    it. Only ``partial`` is counted: a ``success`` stamp is the wrong shape (the
    merge-block gate already downgrades genuine no-ops to ``partial``), and a
    ``failed`` lap is not a costume — it is an honest miss. An honest ``partial``
    that carries none of the undelivered signatures is likewise excluded.

    Two exclusions keep the count honest about *progress* (#2595):

    * The gate's own ``[branch-resolved]`` skip stamps never count — otherwise
      each firing increments the count it gates on and the latch re-arms itself,
      permanently locking the PR out of remediation.
    * When *is_delivered* is supplied, a counted lap whose recorded
      ``pr_head_sha`` it reports delivered (a proper ancestor of the PR's
      current origin head — Tend pushed that lap's resolution) is discounted.
      Laps with no ``pr_head_sha`` (legacy records) and laps the predicate
      reports undelivered keep counting, so genuinely futile re-lapping is still
      caught. The predicate is injected so this function stays pure — the git
      consultation lives in the calling gate, mirroring the injected-oracle
      convention of this module's git-merge-truth sensors. *is_delivered*
      ``None`` (the default) preserves the original unfiltered count.
    """
    count = 0
    for task in tasks:
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        if int(match.group(1)) != pr_number:
            continue
        if task.get("outcome") != "partial":
            continue
        summary = task.get("result_summary")
        if not isinstance(summary, str):
            continue
        lowered = summary.lower()
        # Never let the gate's own skip stamp feed the count it gates on.
        if lowered.startswith(_BRANCH_RESOLVED_SKIP_PREFIX):
            continue
        if not any(
            marker in lowered for marker in _UNDELIVERED_PARTIAL_MARKERS
        ):
            continue
        # Discount laps Tend has already delivered: a recorded head SHA that
        # the injected predicate reports a proper ancestor of the current
        # origin head proves the lap's resolution landed, so it was not a
        # costume. A missing/blank SHA (legacy) keeps counting.
        if is_delivered is not None:
            head_sha = task.get("pr_head_sha")
            if (
                isinstance(head_sha, str)
                and head_sha
                and is_delivered(head_sha)
            ):
                continue
        count += 1
    return count


def _read_recent_task_tail(data_dir: Path, n: int) -> list[dict[str, object]]:
    """Return the trailing *n* parsed records of ``task_history.jsonl``.

    Returns an empty list when *n* is non-positive, the file is missing or
    unreadable, or every tail line is corrupt. Append-order is preserved.
    """
    if n <= 0:
        return []
    path = data_dir / _TASK_HISTORY_FILE
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return []
    records: list[dict[str, object]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def lap_window_start(data_dir: Path, n_cycles: int) -> str | None:
    """Return the oldest ``timestamp`` in the trailing *n_cycles* task records.

    The window start of the same :func:`_read_recent_task_tail` tail that
    :func:`measure_unlanded_remediations` scans, so a count paired with the lap
    measure (the ``TERMINAL-DEEDS`` deed count) can cover the same horizon.
    Returns ``None`` when the tail is empty or carries no string timestamp.
    """
    for task in _read_recent_task_tail(data_dir, n_cycles):
        stamp = task.get("timestamp")
        if isinstance(stamp, str) and stamp:
            return stamp
    return None


# ---------------------------------------------------------------------------
# Version-archive corroboration — a local, host-independent merge-truth signal
# ---------------------------------------------------------------------------
#
# A ``VersionRecord`` is appended to ``version_archive.jsonl`` only when a plan's
# PR actually merged (its commit landed in ``main``), so the record's ``plan_id``
# is a merge-truth signal already on local disk — one the ``is_pr_settled``
# chokepoint never read back into settlement. A squash-merged PR reads not-merged
# by ``git merge-base --is-ancestor`` (the squash commit is not the branch head's
# linear ancestor, exit 1), and when the host ``mergedAt`` is null/unreadable the
# chokepoint's squash override finds nothing either — so the merge is on disk yet
# the remediation lap resumes. This corroboration tier closes that gap.
#
# Matched on ``plan_id`` **exactly** — the durable unique key — never on the PR
# title or number: a prior version cross-referenced the archive by title/number
# and false-flagged PRs whose commit reached ``main`` but whose ``VersionRecord``
# title/number never matched (#2504). An exact ``plan_id`` match cannot recur
# that failure mode.


def version_archive_has_landing(plan_id: str, records: Iterable[object]) -> bool:
    """Return True when *records* carries a landing for *plan_id*.

    Scans an already-loaded iterable of ``VersionRecord``-shaped rows for any
    whose ``plan_id`` equals *plan_id* — the host-independent local corroboration
    the :meth:`~maestro.agent.MaestroAgent.is_pr_settled` chokepoint reads *after*
    its fetch-first ancestry read and host ``mergedAt`` override both leave a PR
    unmerged (see the module comment above). Matches on ``plan_id`` exactly,
    never on title/number (#2504). The caller loads the archive through the
    injected :class:`~maestro.memory.VersionArchiveBackend` (JSONL default or the
    shared SQLite store behind ``[paths] sqlite_state_enabled``), so this stays a
    pure predicate with no ``memory`` dependency and no file I/O of its own —
    ``getattr`` duck-typing keeps it record-shape-agnostic. Returns ``False`` when
    *plan_id* is empty or no record matches. Read-only, zero SDK cost.
    """
    if not plan_id:
        return False
    return any(getattr(r, "plan_id", None) == plan_id for r in records)


def _strip_merge_block_outcomes(
    outcomes: dict[str, list[bool]],
    tasks: list[dict[str, object]],
) -> dict[str, list[bool]]:
    """Return *outcomes* with merge-block-origin no-ops removed per PR (#2796).

    For each PR, counts merge-block-origin laps in *tasks* — a ``partial``
    ``Remediate PR #<n>`` record whose ``result_summary`` carries
    :data:`_MERGE_BLOCK_MARKER` (case-insensitive) — and removes up to that many
    trailing ``True`` (no-op) entries from the PR's outcome list, most-recent
    first. The persisted booleans don't self-describe their origin, so the count
    caps the removal: a PR with no merge-block laps loses nothing, and
    genuinely-attempted no-op history (laps lacking the marker) is preserved, so
    the #2744 guard stays intact. A list emptied of every entry is dropped, so a
    fully-reconciled PR reads as having no history. Pure — the caller persists
    the result.
    """
    mb_counts: dict[str, int] = {}
    for task in tasks:
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        if task.get("outcome") != "partial":
            continue
        summary = task.get("result_summary")
        if not isinstance(summary, str):
            continue
        if _MERGE_BLOCK_MARKER not in summary.lower():
            continue
        key = match.group(1)
        mb_counts[key] = mb_counts.get(key, 0) + 1
    reconciled: dict[str, list[bool]] = {}
    for key, lst in outcomes.items():
        budget = mb_counts.get(key, 0)
        if budget <= 0:
            reconciled[key] = lst
            continue
        kept: list[bool] = []
        for val in reversed(lst):
            if val and budget > 0:
                budget -= 1
                continue
            kept.append(val)
        kept.reverse()
        if kept:
            reconciled[key] = kept
    return reconciled


def measure_unlanded_remediations(
    data_dir: Path,
    n_cycles: int,
    *,
    is_pr_merged: Callable[[int], bool] | None = None,
) -> dict[int, int]:
    """Detect remediation lineages stamped success/partial that never landed.

    Scans the trailing *n_cycles* records of ``task_history.jsonl`` and, for
    each remediation task — one whose goal matches ``Remediate PR #<n>:
    <title>`` and whose outcome is ``success`` or ``partial`` — consults
    git-merge-truth: the PR is *unlanded* when *is_pr_merged* reports its commit
    is **not** reachable from ``main``. *is_pr_merged* defaults to
    :func:`maestro.audit_checks_phantom._git_pr_merged_default` (lazy-imported to avoid
    a circular import, since ``audit_checks`` imports this module). The verdict
    is cached per PR so the oracle runs at most once per distinct PR number.

    Returns a ``{pr_number: stamp_count}`` mapping (sorted ascending) of the
    unlanded lineages found within the recent window. The *n_cycles* bound
    keeps the scan finite: a remediation older than the trailing window is not
    surfaced, so a long-resolved-or-abandoned lineage drops off rather than
    nagging forever. Measurement-only — pure read, zero SDK cost.

    The oracle call is deliberately *not* wrapped in a swallowing handler: an
    *is_pr_merged* that raises propagates loudly rather than being silently
    treated as not-merged (which would mass-flag every lineage). The default
    oracle is itself best-effort fail-closed, so the only raising path is an
    injected predicate — and the loud failure surfaces the broken sensor.
    """
    if n_cycles <= 0:
        return {}
    if is_pr_merged is None:
        from maestro.audit_checks_phantom import (
            _git_pr_merged_default,
        )

        is_pr_merged = _git_pr_merged_default
    merged_cache: dict[int, bool] = {}
    stamps: dict[int, int] = {}
    for task in _read_recent_task_tail(data_dir, n_cycles):
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        outcome = task.get("outcome")
        if (
            not isinstance(outcome, str)
            or outcome not in _REMEDIATION_STAMP_OUTCOMES
        ):
            continue
        pr_number = int(match.group(1))
        if pr_number not in merged_cache:
            merged_cache[pr_number] = is_pr_merged(pr_number)
        if merged_cache[pr_number]:
            continue
        stamps[pr_number] = stamps.get(pr_number, 0) + 1
    return {pr: count for pr, count in sorted(stamps.items())}


# How many trailing ``task_history.jsonl`` records the git-merge-truth
# UNLANDED-REMEDIATION sensor scans. Bounds the scan so a long-abandoned
# remediation lineage drops off rather than nagging forever.
_UNLANDED_REMEDIATION_CYCLES = 50


def _readiness_int(snapshot: dict[str, object], key: str) -> int:
    """Read an integer field from a persisted readiness snapshot, defaulting 0."""
    value = snapshot.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def pr_readiness_improved(
    prev: dict[str, object], cur: dict[str, object],
) -> bool:
    """Return True when at least one code-clearable blocker cleared (089 step 4).

    *prev* and *cur* are the persisted ``dataclasses.asdict`` snapshots of two
    consecutive :class:`~maestro.pr_facts.PRReadiness` observations for the
    same PR (the ``pr_readiness_snapshot`` field of two
    :class:`~maestro.memory.TaskRecord` laps). A lap *made progress* when, since
    the prior snapshot, any one of its code-clearable blockers receded:

    * the PR reached ready-and-waiting (``cur["ready_and_waiting"]``) — the
      terminal in which there is nothing left to clear;
    * fewer unresolved inline threads or unaddressed overflow findings remain;
    * CI advanced to ``GREEN`` from any non-green state;
    * a behind-main or conflicting branch state resolved.

    Defensive against malformed / partial snapshots (missing keys default to the
    no-blocker reading), so a corrupt record never falsely reports progress on a
    count it cannot prove. Pure, zero SDK cost.
    """
    if cur.get("ready_and_waiting"):
        return True
    if _readiness_int(cur, "unresolved_threads") < _readiness_int(
        prev, "unresolved_threads",
    ):
        return True
    if _readiness_int(cur, "unaddressed_overflow") < _readiness_int(
        prev, "unaddressed_overflow",
    ):
        return True
    if prev.get("ci_state") != "GREEN" and cur.get("ci_state") == "GREEN":
        return True
    if prev.get("branch_conflicting") and not cur.get("branch_conflicting"):
        return True
    return bool(prev.get("branch_behind")) and not cur.get("branch_behind")


def measure_no_progress_laps(
    data_dir: Path, n_cycles: int,
) -> dict[int, int]:
    """Count consecutive per-PR remediation laps that cleared no blocker.

    Scans the trailing *n_cycles* records of ``task_history.jsonl`` and, per PR,
    walks its ``Remediate PR #<n>`` laps that carry a ``pr_readiness_snapshot``
    in append order, tracking the length of the *current* run of laps that
    neither reached ready-and-waiting nor improved on the prior lap
    (:func:`pr_readiness_improved`). The run resets to zero **only** on genuine
    readiness progress:

    * a lap that reached ready-and-waiting (nothing left to clear), or made
      progress over the prior snapshot (:func:`pr_readiness_improved`).

    A new ``pr_head_sha`` is deliberately **not** a reset signal. ``run()``
    self-pushes a fresh commit — and therefore a fresh head SHA — on essentially
    every remediation lap (per the "run() pushes remediation fixes to
    ``PendingPRRequest.pr_branch`` directly" invariant), whether or not any
    reviewer-visible blocker was cleared. Treating raw head movement as progress
    would let a self-pushed lap that cleared nothing oscillate the run back to
    zero forever, so the SHA-advancing-but-unlanded treadmill could never reach
    the budget — inverting the progress-aware handoff's intent (#2895). The
    :func:`pr_readiness_improved` check already recognizes a real Tend landing
    that clears a blocker, so genuine progress still resets across head changes.

    Returns a ``{pr_number: consecutive_no_progress_laps}`` mapping (sorted
    ascending) of only the PRs whose latest run is non-empty — a PR clearing a
    fresh blocker every lap (the healthy churn of a fast main) never accumulates,
    while one grinding on identical observations does. Laps without a snapshot
    (legacy / non-remediation records) are skipped. Measurement-only — pure read,
    zero SDK cost; the progress-aware lap-budget handoff (step 5) consumes it.
    """
    if n_cycles <= 0:
        return {}
    counts: dict[int, int] = {}
    prev_snapshot: dict[int, dict[str, object]] = {}
    for task in _read_recent_task_tail(data_dir, n_cycles):
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        snapshot = task.get("pr_readiness_snapshot")
        if not isinstance(snapshot, dict):
            continue
        pr_number = int(match.group(1))
        if snapshot.get("ready_and_waiting") or (
            pr_number in prev_snapshot
            and pr_readiness_improved(prev_snapshot[pr_number], snapshot)
        ):
            # A lap that reached the terminal or cleared a blocker restarts the
            # no-progress run.
            counts[pr_number] = 0
        else:
            counts[pr_number] = counts.get(pr_number, 0) + 1
        prev_snapshot[pr_number] = snapshot
    return {pr: c for pr, c in sorted(counts.items()) if c > 0}


def format_unlanded_remediations_line(
    unlanded: dict[int, int],
    n_cycles: int,
) -> str:
    """Render an ``UNLANDED-REMEDIATION`` line from a measured lap dict, or ``""``.

    Pure formatter — the rendering half of :func:`format_unlanded_remediations`,
    split out so a caller that already holds the ``{pr: count}`` dict from
    :func:`measure_unlanded_remediations` (e.g. one that also feeds it into
    :func:`format_pr_lap_summary`) renders this line *without a second
    measurement pass*. Re-measuring re-runs the host merge-truth oracle (a
    network call memoized only within a single ``measure`` call), so deriving
    both the UNLANDED-REMEDIATION and PR-LAPS lines from one dict keeps the
    oracle at one call per distinct PR per iteration instead of two. Returns
    ``""`` when *unlanded* is empty so the caller injects only when there is
    something to report.
    """
    if not unlanded:
        return ""
    parts = ", ".join(f"#{pr} (x{count})" for pr, count in unlanded.items())
    return (
        f"UNLANDED-REMEDIATION: {len(unlanded)} PR(s) stamped success/partial "
        f"but not merged into main per git-merge-truth within last "
        f"{n_cycles} tasks: {parts}"
    )


def format_unlanded_remediations(
    data_dir: Path,
    n_cycles: int,
    *,
    is_pr_merged: Callable[[int], bool] | None = None,
) -> str:
    """Render an ``UNLANDED-REMEDIATION`` line, or ``""`` when none.

    Convenience wrapper that calls :func:`measure_unlanded_remediations` once
    and renders the result with :func:`format_unlanded_remediations_line`. A
    caller that also needs the lap dict for :func:`format_pr_lap_summary` should
    measure once itself and call the line formatter directly, rather than this
    wrapper plus a second measurement, to avoid re-running the host merge-truth
    oracle. Pure read — zero SDK cost.
    """
    return format_unlanded_remediations_line(
        measure_unlanded_remediations(
            data_dir, n_cycles, is_pr_merged=is_pr_merged,
        ),
        n_cycles,
    )


def format_pr_lap_summary(
    lap_counts: dict[int, int],
    standdown_prs: Collection[int],
    budget: int,
    *,
    refuted_prs: Collection[int] = (),
) -> str:
    """Render a ``PR-LAPS`` line surfacing PRs nearing the lap budget, or ``""``.

    *lap_counts* maps ``pr_number`` → cumulative unlanded remediation laps (the
    ``{pr: count}`` from :func:`measure_unlanded_remediations`); *standdown_prs*
    is the set of PRs carrying a durable handoff standdown (from
    :func:`maestro.escalation_bridge.active_standdown_prs`); *budget* is the
    configured ``[loop] pr_remediation_lap_budget``.

    A PR surfaces when its laps reach **half the budget** (``2 * laps >=
    budget``) — so a pileup is observable in contemplation long before it
    reaches the budget and thrashes like #2579's 44 laps — or when it carries an
    active standdown (handed up, minting suppressed), shown even at zero laps in
    the current window so the operator still sees a suppressed PR. Each surfaced
    PR is annotated ``[stood-down]`` when a standdown is active.

    *refuted_prs* (proposal 135) names the stood-down PRs whose latched claim the
    live reading **contradicts** — measured by
    :func:`~maestro.standdown_validity.measure_contradicted_standdowns`, which
    folds each record's claim against the owed act derived from that PR's latest
    persisted readiness snapshot. Such a PR is annotated ``[standdown-refuted]``
    *in place of* ``[stood-down]``: the latch says only Thomas can act, while the
    live reading says owned, code-clearable work is owed. The annotation is
    deliberately **not** spelled as a ``[stood-down…]`` variant — this module's
    :data:`_CHOSEN_QUIET_MARKER` (``"[stood-down]"``) is matched by *substring*
    over ``result_summary`` / ``key_findings`` by
    :func:`classify_remediation_outcome_state` and
    :func:`measure_forced_vs_chosen_quiet` to score a stand-down as
    chosen-rather-than-forced, so a refutation annotation sharing that prefix
    would let a contradicted latch be scored as a deliberate quiet act.
    ``[standdown-refuted]`` shares no substring with the marker.

    Defaults to empty, so a caller that measures no contradiction renders exactly
    the pre-135 line.

    Returns ``""`` when *budget* is non-positive (the feature is off) or no PR
    qualifies, so the caller injects only when there is something to report.
    Pure — zero SDK cost; the caller does the I/O (lap measurement and standdown
    read), mirroring :func:`format_unlanded_remediations`' division of labor.
    """
    if budget <= 0:
        return ""
    standdown_set = {int(pr) for pr in standdown_prs}
    refuted_set = {int(pr) for pr in refuted_prs}
    parts: list[str] = []
    for pr in sorted(set(lap_counts) | standdown_set):
        laps = lap_counts.get(pr, 0)
        stood_down = pr in standdown_set
        if 2 * laps < budget and not stood_down:
            continue
        tag = ""
        if stood_down:
            tag = (
                " [standdown-refuted]"
                if pr in refuted_set
                else " [stood-down]"
            )
        parts.append(f"#{pr} ({laps}/{budget} laps){tag}")
    if not parts:
        return ""
    return (
        "PR-LAPS: remediation laps nearing the per-PR budget — drive to "
        f"merge/close or stand down: {', '.join(parts)}"
    )


def _blocker_class_from_snapshot(snapshot: dict[str, object]) -> str:
    """Map a persisted ``pr_readiness_snapshot`` to a greenness blocker class.

    Reads the snapshot's blocker dimensions in **severity-descending** order —
    the reddest present blocker names the lap's class, mirroring the
    :func:`maestro.remediation_tracker.classify_merge_block` precedence:

    * a conflicting branch → :data:`~maestro.pr_facts.CONFLICTING`;
    * else a failing CI rollup → :data:`~maestro.pr_facts.CI_FAILING`;
    * else any unresolved inline thread *or* unaddressed overflow finding →
      :data:`~maestro.pr_facts.THREADS_UNRESOLVED` (both are owned,
      code-clearable review work);
    * else :data:`~maestro.pr_facts.GREEN` (no code-clearable blocker in
      the total order — a behind-main-only lap, which is mechanically Tend-cleared
      and absent from the order, reads here as green).

    Defensive against malformed / partial snapshots (missing keys read as the
    no-blocker value), so a corrupt record never invents a redder class than it
    can prove. Pure, zero SDK cost.
    """
    if snapshot.get("branch_conflicting"):
        return CONFLICTING
    if snapshot.get("ci_state") == "FAILING":
        return CI_FAILING
    if _readiness_int(snapshot, "unresolved_threads") > 0 or _readiness_int(
        snapshot, "unaddressed_overflow",
    ) > 0:
        return THREADS_UNRESOLVED
    return GREEN


def measure_greenness_stall(
    data_dir: Path, n_cycles: int,
) -> dict[int, int]:
    """Sum each PR's cumulative blocker-greenness movement over the window.

    Scans the trailing *n_cycles* records of ``task_history.jsonl`` and, per PR,
    walks its ``Remediate PR #<n>`` laps that carry a ``pr_readiness_snapshot`` in
    append order, mapping each lap to a blocker class
    (:func:`_blocker_class_from_snapshot`) and summing
    :func:`~maestro.pr_facts.greenness_delta` across every consecutive
    pair. The result is the PR's **net greenness gain**: how far its blocker moved
    down the severity total order (toward clear) across the window.

    Returns a ``{pr_number: cumulative_gain}`` mapping (sorted ascending) over
    every PR with at least one snapshot-bearing lap. A gain of **zero** is the
    mechanical signature of a *stall* — a lineage grinding at the same severity
    class (an all-``CONFLICTING`` churn nets 0), the ``blocker-rotation-not-
    clearance`` limitation made measurable — while a **positive** gain is a
    *rotation toward greener* (``CONFLICTING → CI_FAILING`` nets ``+1``): the
    blocker got code-clearable, so the honest next act is to go clear the red, not
    retire. A single-lap PR nets 0 (no movement observed yet).

    Laps without a snapshot (legacy / non-remediation records) are skipped.
    Measurement-only — pure read, zero SDK cost; :func:`format_pr_churn_summary`
    consumes it to split PR-STUCK (stalled → retire) from PR-ROTATING (greener →
    fix the red).
    """
    if n_cycles <= 0:
        return {}
    gain: dict[int, int] = {}
    prev_class: dict[int, str] = {}
    for task in _read_recent_task_tail(data_dir, n_cycles):
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        snapshot = task.get("pr_readiness_snapshot")
        if not isinstance(snapshot, dict):
            continue
        pr_number = int(match.group(1))
        cur_class = _blocker_class_from_snapshot(snapshot)
        gain.setdefault(pr_number, 0)
        if pr_number in prev_class:
            gain[pr_number] += greenness_delta(prev_class[pr_number], cur_class)
        prev_class[pr_number] = cur_class
    return {pr: g for pr, g in sorted(gain.items())}


def latest_pr_readiness_snapshots(
    task_history: list[dict[str, object]],
    *,
    not_before: Mapping[int, str] | None = None,
) -> dict[int, dict[str, object]]:
    """Return each PR's latest persisted ``pr_readiness_snapshot`` from *task_history*.

    Walks the in-memory task records in order, matching
    :data:`_REMEDIATION_GOAL_PATTERN` against each record's ``goal`` (falling
    back to ``title`` when the goal is missing or empty, as a remediation record
    written by an older path carries its subject in the title), and keys the
    record's ``pr_readiness_snapshot`` by the matched PR number. **Last write
    wins**, so the result is the most recent observation per PR. A record whose
    goal does not name a PR, or whose snapshot is missing or not a dict, is
    skipped.

    *not_before* (proposal 135) optionally names, per PR, an ISO timestamp a
    snapshot must not predate. A PR listed there contributes only from records
    whose own ``timestamp`` parses and is **at or after** that instant; an
    older record is skipped, so the reading is not silently stale. A PR absent
    from the map, or one whose *not_before* value is unparseable, is unfiltered
    (the tolerant direction: an unreadable bound must not blank the reading).
    The stand-down contradiction sensor passes each latch's ``delivered_at``
    through it, because a snapshot written *before* a claim was latched cannot
    refute that claim — snapshots stop being stamped once the latch suppresses
    the remediation that would write them, so without this bound the "live"
    reading is stale by construction.

    Lifted out of :func:`format_pr_churn_summary`, which now reads through it, so
    the PR-churn line and the stand-down contradiction sensor (proposal 135)
    share one reader of *latest readiness per PR* rather than re-inlining the
    walk. Pure, zero SDK cost — the caller supplies the history.
    """
    floors = {
        pr: floor
        for pr, raw in (not_before or {}).items()
        if (floor := _parse_iso_timestamp(raw)) is not None
    }
    latest: dict[int, dict[str, object]] = {}
    for task in task_history:
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        snapshot = task.get("pr_readiness_snapshot")
        if not isinstance(snapshot, dict):
            continue
        pr_number = int(match.group(1))
        floor = floors.get(pr_number)
        if floor is not None:
            observed = _parse_iso_timestamp(task.get("timestamp"))
            if observed is None or observed < floor:
                continue
        latest[pr_number] = snapshot
    return latest


def format_pr_churn_summary(
    task_history: list[dict[str, object]],
    no_progress_laps: dict[int, int],
    lap_budget: int,
    data_dir: Path,
    *,
    settled_prs: Callable[[], frozenset[int]],
    is_pr_settled: Callable[[int], bool],
) -> str:
    """Render the PR-READY / PR-STUCK / PR-ROTATING churn block, or ``""``.

    Proposal 089 step 6, extended with the greenness total order. Folds three
    distinct PR-churn signals into one display so contemplation can tell *done*
    from *stalled* from *rotating-greener* from *healthily churning*:

    * **PR-READY #N** — the PR's latest ``pr_readiness_snapshot`` in *task_history*
      reports ready-and-waiting: every code-clearable blocker is clear and the
      work is done, awaiting Thomas's approval or Tend's push. A clean terminal
      to recognize, not a loop to chase.
    * **PR-ROTATING #N** — the PR's blocker moved *strictly greener* across the
      window (net :func:`measure_greenness_stall` gain > 0, e.g. ``CONFLICTING →
      CI_FAILING``) **and its latest snapshot is not ready-and-waiting**: it is
      not stuck, it rotated toward code-clearable work, so the honest next act is
      to go clear the (now-greener) red, not retire. A trajectory that rotated
      all the way to ready (``CONFLICTING → … → ready``) nets positive gain too,
      but there is no red left to clear — it reads as PR-READY, never
      PR-ROTATING, so the two lines never contradict each other for one PR.
    * **PR-STUCK #N** — the PR's consecutive no-progress lap count (*no_progress_laps*
      from :func:`measure_no_progress_laps`) has reached *lap_budget* **and** its
      net greenness gain is zero-or-negative: it is grinding at the same severity
      class, genuinely stalled (an all-``CONFLICTING`` churn), so drive to
      merge/close or hand up.

    Splitting PR-STUCK on greenness is the whole point: a rotation toward greener
    and a same-class stall used to read as one undifferentiated "stuck" word. A
    PR churning *with* readiness progress (clearing a freshly-arrived blocker each
    lap) raises none of the loop lines — it is doing the job of a fast main. The
    STUCK and ROTATING states are mutually exclusive by construction (STUCK
    requires gain ≤ 0, ROTATING requires gain > 0).

    Reads greenness from *data_dir* via :func:`measure_greenness_stall`; the
    caller measures *no_progress_laps* and passes the in-memory *task_history*,
    mirroring :func:`format_pr_lap_summary`'s division of labor. Zero SDK cost.
    Returns ``""`` when no signal fires so the caller injects only when there is
    something to report.

    The block is per *open* PR: every PR in the set *settled_prs* returns (the
    settlement ledger's effectively settled set, read fetch-free) is skipped in
    all three branches; the thunk is called at most once and only when some
    line would otherwise render, so a quiet block pays no ledger read. A
    merged or host-closed PR's last pre-merge snapshot or lap count therefore
    cannot keep rendering after it lands. Coverage is
    bounded by the ledger: only PRs ``reconcile_settled_verdicts`` has upserted
    are filtered by it.

    A PR the ledger does not name is then checked against the merge-truth
    chokepoint (#4596): *is_pr_settled* is required (no default) so no caller
    can render the block without naming a settlement oracle. It is queried
    lazily, once per PR, only for PRs that would otherwise emit a line and the
    ledger did not already settle; an oracle error propagates (fail loud),
    mirroring :func:`measure_unlanded_remediations`.
    """
    latest_snapshot = latest_pr_readiness_snapshots(task_history)
    greenness_gain = measure_greenness_stall(data_dir, _UNLANDED_REMEDIATION_CYCLES)
    lines: list[tuple[int, str]] = []
    for pr in sorted(latest_snapshot):
        if latest_snapshot[pr].get("ready_and_waiting"):
            lines.append((pr,
                f"PR-READY #{pr}: every code-clearable blocker is clear — the "
                "work is done and the PR awaits approval/push. A clean terminal "
                "to recognize, not a loop to chase.",
            ))
    for pr in sorted(greenness_gain):
        if greenness_gain[pr] > 0 and not latest_snapshot.get(pr, {}).get(
            "ready_and_waiting"
        ):
            lines.append((pr,
                f"PR-ROTATING #{pr}: blocker moved strictly greener "
                f"(+{greenness_gain[pr]} on the severity order) — not stalled, "
                "rotated toward code-clearable work; go clear the red, don't "
                "retire.",
            ))
    if lap_budget > 0:
        for pr in sorted(no_progress_laps):
            laps = no_progress_laps[pr]
            if laps >= lap_budget and greenness_gain.get(pr, 0) <= 0:
                lines.append((pr,
                    f"PR-STUCK #{pr}: {laps} consecutive lap(s) cleared no "
                    f"blocker (budget {lap_budget}) with no greenness gain — "
                    "genuinely stalled at the same severity, not rotating; "
                    "drive to merge/close or hand up.",
                ))
    if not lines:
        return ""
    ledger_settled = settled_prs()
    settled_cache: dict[int, bool] = {}

    def _live(pr: int) -> bool:
        if pr in ledger_settled:
            return False
        if pr not in settled_cache:
            settled_cache[pr] = is_pr_settled(pr)
        return not settled_cache[pr]

    return "\n".join(line for pr, line in lines if _live(pr))


# ---------------------------------------------------------------------------
# Forced-read directive bridge — the unlanded sensor turned into a boundary
# ---------------------------------------------------------------------------
#
# Every sibling above this line *measures* the wheel; none break it. This
# actuator consumes the same git-merge-truth oracle the unlanded sensor reads
# and routes it into ``next_task_directive``. When a ``Remediate PR #N``
# lineage has been serviced ≥ ``threshold`` times in the recent window yet git
# shows the PR unmerged, it forbids a *third* silent remediation lap: the next
# task must first read PR #N's real merge blocker across four oracles (host
# state, merge-truth grep, head-SHA ancestry, branch search), classify it as
# mine-vs-Tend, and drive to merge or go silent — never spin a fresh
# remediation. Reading the real blocker is the one act the self-model names as
# repeatedly avoided; this codifies it as a structural boundary so future-me
# cannot re-lap blind. Self-extinguishing: once git-truth shows the PR merged
# it drops below threshold (or is no longer unmerged) and the bridge stops
# firing. Experimental — it actuates; revert if it never measurably changes the
# next task's shape.

# Hard cap mirroring ``SelfModel.next_task_directive`` (one-shot, ≤200 chars).
_FORCED_READ_DIRECTIVE_MAX = 200


def check_forced_read_directive(
    tasks: list[dict[str, object]],
    model: object,
    *,
    is_pr_merged: Callable[[int], bool],
    threshold: int = 2,
) -> str | None:
    """Return a forced-read directive for a serviced-but-unlanded PR, or None.

    Fires only when **all** hold:

    * ``model.next_task_directive`` is empty (this is a low-precedence bridge
      that never overwrites a directive a higher-precedence gate already set);
    * some ``Remediate PR #N`` lineage appears ≥ *threshold* times across
      *tasks* (the recent task-history window), counting every occurrence
      regardless of outcome — the question is how often the *router* re-fed the
      PR, not whether each lap claimed progress;
    * merge truth — via *is_pr_merged* — shows that PR is **not** settled.

    When several PRs qualify, the lowest-numbered (oldest) is chosen, matching
    the oldest-bug-first doctrine. The returned string instructs a four-oracle
    read of the PR's real merge blocker, a mine-vs-Tend classification, and a
    drive-to-merge-or-go-silent with no fresh remediation lap.

    *is_pr_merged* is required: callers inject the ``is_pr_settled``
    chokepoint (merged *or* closed), so there is no silent fallback to a
    merged-only git-subject read. The merge verdict is consulted only for PRs
    at or above *threshold* and is cached per PR, so the oracle runs at most
    once per distinct candidate. The oracle call is deliberately not wrapped
    in a swallowing handler: a raising predicate propagates loudly rather than
    being treated as not-merged.

    Pure, read-only, zero SDK cost. Returns ``None`` when nothing qualifies.
    Self-extinguishing: once the oracle reports the PR settled it no longer
    surfaces.
    """
    if threshold <= 0:
        return None
    if getattr(model, "next_task_directive", None):
        return None
    counts: dict[int, int] = {}
    for task in tasks:
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        pr_number = int(match.group(1))
        counts[pr_number] = counts.get(pr_number, 0) + 1
    candidates = sorted(pr for pr, c in counts.items() if c >= threshold)
    if not candidates:
        return None
    for pr_number in candidates:
        if is_pr_merged(pr_number):
            continue
        return _forced_read_directive_text(pr_number, counts[pr_number])
    return None


def _forced_read_directive_text(pr_number: int, serviced: int) -> str:
    """Render the forced-read directive for *pr_number*, capped at 200 chars."""
    directive = (
        f"PR #{pr_number} serviced {serviced}x, unmerged. Read its real "
        f"blocker via 4 oracles (GH state, merge-truth grep, head-SHA "
        f"ancestry, branch search), classify mine-vs-Tend, then merge or go "
        f"silent — no new lap."
    )
    if len(directive) > _FORCED_READ_DIRECTIVE_MAX:
        directive = directive[: _FORCED_READ_DIRECTIVE_MAX - 1] + "…"
    return directive


# ---------------------------------------------------------------------------
# Pending-PR admission provenance — readmission-channel sensor
# ---------------------------------------------------------------------------
#
# ``pending_pr_requests`` is a FIFO-exempt queue: PR remediation goals bypass
# the normal task queue (``pending_user_requests`` → ``pending_pr_requests`` →
# queue head). PRs are admitted into it via the *tend-route* append — new PR
# feedback routed by ``tend_evolution_prs``. Nothing measured how often the
# *same* PR is admitted repeatedly, so the 130-evolution assertion that this
# bypass mechanically explains the remediation monopoly went untested — an
# unmeasured channel.
#
# ``log_pending_pr_provenance`` instruments that seam; the sensor below
# consumes the append-only log to count re-admitted PRs. Measurement-only — it
# records the admission, it does not gate it.

# Append-only JSONL written by :func:`log_pending_pr_provenance`.
_PR_PROVENANCE_FILE = "pending_pr_provenance.jsonl"

# Admission-channel label recorded by :func:`log_pending_pr_provenance`.
PR_ADMISSION_CHANNEL_TEND = "tend_route"

# A PR admitted this many distinct times (or more) is "re-admitted" — the
# channel keeps re-routing it rather than landing it once.
_PR_READMISSION_THRESHOLD = 2

# Trailing-window bound for the readmission scan. Without it the sensor reads
# the entire (never-archived) provenance log, so once any PR crosses the
# threshold the PR-READMISSION line latches into every prompt forever — even
# after that PR has merged and the bypass has stopped, defeating the sensor's
# falsification purpose (#2531 review). Mirrors the trailing windows on the
# sibling sensors (measure_unlanded_remediations / measure_false_success_recurrence)
# so a long-resolved PR ages out of the window rather than nagging forever.
_PR_READMISSION_WINDOW = 500


@dataclass(frozen=True)
class ProvenanceRecord:
    """One ``pending_pr_requests`` admission — a PR number and its channel.

    Mirrors the on-disk ``pending_pr_provenance.jsonl`` row exactly
    (``{"pr_number", "channel"}``). There is no timestamp, so — unlike the
    stamped deed / backlog ledgers — no shared ``build_*`` constructor is needed;
    the writer stamps nothing. ``pr_number`` is always positive: the writer and
    every reader drop non-positive admissions as phantom signal.
    """

    pr_number: int
    channel: str


def _read_provenance_lines(data_dir: Path) -> list[str]:
    """Read the raw ``pending_pr_provenance.jsonl`` lines, or ``[]``.

    Returns an empty list when the log is absent or unreadable — the sensor
    fails open to zero counts rather than crashing the zero-cost path — matching
    the readmission sensor's original tolerance.
    """
    path = data_dir / _PR_PROVENANCE_FILE
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return list(f)
    except OSError:
        return []


def _provenance_record_from_row(parsed: object) -> ProvenanceRecord | None:
    """Return a :class:`ProvenanceRecord` from a decoded row, or ``None``.

    A row is dropped when it is not a dict or lacks a positive integer
    ``pr_number`` (``bool`` is rejected because it subclasses ``int``), mirroring
    the original inline guard. A missing or non-string ``channel`` is coerced to
    ``""`` so the row is still counted by its ``pr_number`` — the summary groups
    on the number alone, so this keeps byte-for-byte parity with the pre-record
    counting path.
    """
    if not isinstance(parsed, dict):
        return None
    pr_number = parsed.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return None
    if pr_number <= 0:
        return None
    channel = parsed.get("channel")
    if not isinstance(channel, str):
        channel = ""
    return ProvenanceRecord(pr_number=pr_number, channel=channel)


def _parse_provenance_lines(lines: list[str]) -> list[ProvenanceRecord]:
    """Parse raw JSONL lines into records, skipping malformed / invalid rows.

    Preserves the original reader's corrupted-tail tolerance: blank lines and
    lines that do not decode to a valid admission row are silently skipped. File
    order is preserved, so the result is oldest-first.
    """
    records: list[ProvenanceRecord] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        record = _provenance_record_from_row(parsed)
        if record is None:
            continue
        records.append(record)
    return records


def summarize_readmission_records(
    records: list[ProvenanceRecord],
) -> dict[str, int]:
    """Group admission *records* by PR number and count re-admissions.

    Substrate-agnostic core shared by the JSONL and SQLite readers: given
    already-parsed :class:`ProvenanceRecord`s (from either backend) it returns
    the same three-key summary the PR-READMISSION sensor has always produced:

    * ``total_admissions`` — total admission records.
    * ``distinct_prs`` — number of distinct PR numbers admitted.
    * ``readmitted_prs`` — PRs admitted :data:`_PR_READMISSION_THRESHOLD` or more
      times (the channel re-routed the same PR repeatedly).
    """
    counts: dict[int, int] = {}
    for record in records:
        counts[record.pr_number] = counts.get(record.pr_number, 0) + 1
    readmitted = sum(
        1 for c in counts.values() if c >= _PR_READMISSION_THRESHOLD
    )
    return {
        "total_admissions": sum(counts.values()),
        "distinct_prs": len(counts),
        "readmitted_prs": readmitted,
    }


def load_all_provenance_records(data_dir: Path) -> list[ProvenanceRecord]:
    """Return every admission record oldest-first — the SQLite seed source.

    Reads the whole ``pending_pr_provenance.jsonl`` in file (append / oldest
    -first) order with the shared corrupted-tail tolerance. Seeding the SQLite
    store in chronological order lets its ``AUTOINCREMENT`` ``id`` reproduce the
    append order, so a later ``ORDER BY id DESC`` read matches the JSONL
    trailing-window walk. A real call site (the
    :class:`~maestro.state_store.SqlitePrProvenanceLedger` one-time seed), so it
    does not trip the self-review uncalled-symbol gate.
    """
    return _parse_provenance_lines(_read_provenance_lines(data_dir))


def pr_number_from_remediation_goal(goal: str) -> int | None:
    """Extract the PR number from a ``Remediate PR #<n>: ...`` goal/directive.

    Returns ``None`` when *goal* is empty or does not start with the unified
    remediation header. Used to recover the PR number from a composed
    remediation directive that carries only the directive text, not a
    structured PR number (e.g. the no-escalation-bridge stand-down check in
    ``escalation_bridge``).
    """
    if not goal:
        return None
    match = _REMEDIATION_GOAL_PATTERN.match(goal)
    if match is None:
        return None
    return int(match.group(1))


def recent_planning_tasks(
    memory: Memory, config: Config, n: int = 30
) -> list[TaskRecord]:
    """Return evolution planning's recent-task window, oldest first.

    Under ``[loop] planning_excludes_remediation_enabled`` every
    ``Remediate PR #<n>:`` lap is dropped: a lap's kept branch is an open PR
    head, not a promotable task branch. Flag-off returns the read unchanged.
    """
    recent = memory.get_recent_tasks(n)
    if not config.planning_excludes_remediation_enabled:
        return recent
    return [
        t for t in recent if pr_number_from_remediation_goal(t.goal or "") is None
    ]


def planning_base_branch(
    record: TaskRecord | None, config: Config
) -> str | None:
    """Return the kept branch ``implement()`` may base a plan on, or None.

    Under ``[loop] planning_excludes_remediation_enabled`` a remediation lap's
    branch is refused here too, so a plan minted before the flag flip that
    cites a lap demotes to fresh-from-main instead of stacking on a PR head.
    """
    if record is None:
        return None
    if (
        config.planning_excludes_remediation_enabled
        and pr_number_from_remediation_goal(record.goal or "") is not None
    ):
        return None
    return record.branch


def log_pending_pr_provenance(
    data_dir: Path, pr_number: int, channel: str,
) -> None:
    """Append one ``pending_pr_requests`` admission record.

    Records that PR *pr_number* was admitted into ``pending_pr_requests`` via
    *channel* (:data:`PR_ADMISSION_CHANNEL_TEND`). One JSONL line per admission
    is appended to ``pending_pr_provenance.jsonl`` under *data_dir*, guarded by
    :func:`~maestro.fileutil.file_lock`. Append-only; consumed read-only by
    :func:`format_pr_readmission_combined` via
    :func:`summarize_readmission_records`.

    A non-positive *pr_number* carries no channel signal (a synthetic or
    unresolved PR), so it is ignored rather than logged — the sensor must not
    count phantom admissions.
    """
    if pr_number <= 0:
        return
    path = data_dir / _PR_PROVENANCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"pr_number": pr_number, "channel": channel})
    with file_lock(path), open(path, "a", encoding="utf-8") as f:
        f.write(record + "\n")


@runtime_checkable
class PrProvenanceBackend(Protocol):
    """The PR-admission provenance persistence surface (proposal 106).

    One writer (the tend-route admission in ``loop.pr_remediation``) and one
    reader (:func:`format_pr_readmission_combined`) go through this seam, so the
    ledger can live in ``pending_pr_provenance.jsonl`` or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. Mirrors the
    :class:`~maestro.deed_ledger.DeedLedgerBackend` idiom; ``@runtime_checkable``
    enables a structural ``isinstance`` check in the agent's backend-selection
    tests.
    """

    def record(self, pr_number: int, channel: str) -> None:
        """Append one admission record (a non-positive *pr_number* is dropped)."""
        ...

    def recent(self, n: int) -> list[ProvenanceRecord]:
        """Return the *n* most-recent admission records, newest-first."""
        ...


class JsonlPrProvenanceLedger:
    """JSONL-backed :class:`PrProvenanceBackend` — the default, flag-off store.

    Binds a ``data_dir`` and delegates ``record`` to the existing module-level
    :func:`log_pending_pr_provenance` — which stays the JSONL write core, so the
    writer's non-positive-``pr_number`` guard and the shared
    ``tests/test_remediation_tracker.py`` coverage keep one code path. ``recent``
    returns the trailing *n* **valid records** newest-first — it parses before
    taking the window, so a malformed line inside the window never shrinks the
    result below the SQLite backend's last-*n*-valid rows (the proposal 106
    substrate-parity contract), with the shared corrupted-tail tolerance.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def record(self, pr_number: int, channel: str) -> None:
        log_pending_pr_provenance(self._data_dir, pr_number, channel)

    def recent(self, n: int) -> list[ProvenanceRecord]:
        if n <= 0:
            return []
        # Parse *before* windowing so the trailing window holds the last ``n``
        # VALID records — not the last ``n`` raw lines, which a malformed line
        # inside the window would thin out, diverging from the SQLite backend's
        # "last n valid rows" (proposal 106 substrate-parity contract).
        records = _parse_provenance_lines(
            _read_provenance_lines(self._data_dir),
        )[-n:]
        records.reverse()
        return records


# ---------------------------------------------------------------------------
# PR-readmission-rate detector — the wheel measured at the router, not a gate
# ---------------------------------------------------------------------------
#
# ``measure_unlanded_remediations`` answers "which remediations claimed
# progress yet nothing landed?". This sensor answers a sharper question about
# the *router*: which already-merged PRs does the dequeue keep re-feeding as
# *new* ``Remediate PR #<n>`` tasks? A PR git-merge-truth shows MERGED that
# nonetheless re-appears as a remediation task timestamped *after* its merge
# commit is *readmitted-merged* — the router re-serviced work that already
# landed. Laps that all precede the merge are the healthy case (a PR that took
# several laps while open, then landed) and count toward the denominator only. The readmission rate
# (readmitted-merged PRs over all distinct remediated PRs) is the dimensionless
# pulse of that wheel: 0.0 means the router never re-feeds a merged PR; a rising
# value means merged PRs keep cycling back. Pure measurement — it names the
# offending PR numbers and the ratio, and does nothing else. No gate, no
# actuator, no eviction; the cure for the wheel lives upstream at the dequeue,
# not in this sensor.


@dataclass(frozen=True)
class PRReadmissionRate:
    """Result of :func:`measure_pr_readmission_rate`.

    Attributes
    ----------
    ratio:
        ``readmitted_merged_prs / total_distinct_remediated_prs`` — the fraction
        of distinct remediated PRs that git-merge-truth shows merged yet were
        re-admitted as a remediation task timestamped after the merge. ``0.0`` when there are no
        distinct remediated PRs or no readmissions.
    readmitted_prs:
        The offending PR numbers (merged-yet-readmitted), sorted ascending.
    total_distinct_prs:
        Count of distinct PR numbers seen across all ``Remediate PR #<n>``
        task entries — the denominator of :attr:`ratio`.
    """

    ratio: float
    readmitted_prs: tuple[int, ...]
    total_distinct_prs: int


def _lap_admitted_at(task: dict[str, object]) -> datetime | None:
    """Return when a remediation lap was admitted, or ``None`` if unknowable.

    ``TaskRecord.timestamp`` is stamped when the lap is *recorded* (its end),
    so a lap admitted before the merge that finishes after it would otherwise
    read as a post-merge readmission. Subtract a non-negative numeric
    ``duration_secs`` to recover the admission time; fall back to the record
    timestamp when the duration is absent or malformed.
    """
    recorded_at = _parse_iso_timestamp(task.get("timestamp"))
    if recorded_at is None:
        return None
    duration = task.get("duration_secs")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration >= 0
    ):
        return recorded_at - timedelta(seconds=duration)
    return recorded_at


def measure_pr_readmission_rate(
    tasks: list[dict[str, object]],
    *,
    pr_merged_at: Callable[[int], datetime | None] | None = None,
) -> PRReadmissionRate:
    """Measure how often the router re-admits an already-merged PR.

    Scans *tasks* in the order given (chronological append order) for entries
    whose ``goal`` (or ``title`` fallback) matches ``Remediate PR #<n>:
    <title>``, parses the PR number, and groups occurrences by PR. A PR is
    *readmitted-merged* when it appears as a remediation task **more than once**
    (so a later remediation re-admitted it) **and** git-merge-truth — via
    *pr_merged_at* — shows its commit reached ``main`` **and** at least one of
    its laps was *admitted* (``timestamp`` minus ``duration_secs``) strictly
    later than that merge time. Laps
    that all precede the merge are a PR that took several laps while open and
    then landed — not a readmission — so they count toward the denominator
    only. A lap with a missing or unparseable ``timestamp`` cannot prove it
    followed the merge and never makes a PR readmitted (fail-closed). Unlike
    :func:`measure_unlanded_remediations`, occurrences are **not** filtered by
    outcome: the question is whether the *router* re-fed the PR at all, not
    whether each lap claimed progress.

    *pr_merged_at* defaults to
    :func:`maestro.audit_checks_phantom._git_pr_merged_at_default` (lazy-imported to avoid
    a circular import, since ``audit_checks`` imports this module). The merge
    time is consulted only for PRs that re-appear (count ≥ 2), so the oracle
    runs at most once per distinct re-admitted PR.

    Returns a :class:`PRReadmissionRate` whose ``ratio`` is the number of
    readmitted-merged PRs over the number of distinct remediated PRs — ``0.0``
    when there are no distinct remediated PRs or no readmissions. Pure read,
    zero SDK cost; **measurement-only** — no gate, actuator, or eviction.

    The oracle call is deliberately *not* wrapped in a swallowing handler: an
    *pr_merged_at* that raises propagates loudly rather than being silently
    treated as not-merged. The default oracle is itself best-effort fail-closed.
    """
    if pr_merged_at is None:
        from maestro.audit_checks_phantom import (
            _git_pr_merged_at_default,
        )

        pr_merged_at = _git_pr_merged_at_default

    counts: dict[int, int] = {}
    # Latest lap *admission* time per PR (see ``_lap_admitted_at``).
    latest_lap: dict[int, datetime] = {}
    first_seen_order: list[int] = []
    for task in tasks:
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        pr_number = int(match.group(1))
        if pr_number not in counts:
            first_seen_order.append(pr_number)
        counts[pr_number] = counts.get(pr_number, 0) + 1
        lap_at = _lap_admitted_at(task)
        if lap_at is not None:
            prior = latest_lap.get(pr_number)
            if prior is None or lap_at > prior:
                latest_lap[pr_number] = lap_at

    total_distinct = len(counts)
    if total_distinct == 0:
        return PRReadmissionRate(
            ratio=0.0, readmitted_prs=(), total_distinct_prs=0,
        )

    readmitted: list[int] = []
    for pr_number in first_seen_order:
        if counts[pr_number] < 2:
            continue
        # ``first_seen_order`` holds each PR once, so one oracle call per PR.
        merged_at = pr_merged_at(pr_number)
        last_lap = latest_lap.get(pr_number)
        if merged_at is not None and last_lap is not None and last_lap > merged_at:
            readmitted.append(pr_number)

    readmitted_sorted = tuple(sorted(readmitted))
    ratio = len(readmitted_sorted) / total_distinct
    return PRReadmissionRate(
        ratio=ratio,
        readmitted_prs=readmitted_sorted,
        total_distinct_prs=total_distinct,
    )


def format_pr_readmission_combined(
    provenance: PrProvenanceBackend,
    tasks: list[dict[str, object]],
    *,
    pr_merged_at: Callable[[int], datetime | None] | None = None,
) -> str:
    """Render one combined ``PR-READMISSION`` line, or ``""`` when none qualify.

    Folds the two overlapping PR-readmission measurements into a single
    contemplation line so the admission-channel signal and the
    merged-re-serviced rate no longer cost two separate prompt lines
    (contemplation context-bloat is a load-bearing concern). Both numeric
    intents are preserved and no measurement is lost — the admission window is
    summarized by :func:`summarize_readmission_records` and the
    :func:`measure_pr_readmission_rate` function stays intact, read together
    here:

    * The *channel* term (left of the ``/``) is the count of PRs re-admitted
      ≥ :data:`_PR_READMISSION_THRESHOLD` times into ``pending_pr_requests`` via
      the FIFO-exempt bypass, read from the *provenance* backend's trailing
      :data:`_PR_READMISSION_WINDOW` records (JSONL sidecar or ``state.db``,
      chosen behind ``[paths] sqlite_state_enabled``) and summarized by
      :func:`summarize_readmission_records`.
    * The *rate* term (right of the ``/``) is the count of already-merged PRs
      (git-merge-truth via *pr_merged_at*) re-serviced as ``Remediate PR #<n>``
      tasks timestamped after the merge, plus the ratio over distinct remediated PRs, read from
      *tasks* by :func:`measure_pr_readmission_rate`.

    *pr_merged_at* is forwarded to :func:`measure_pr_readmission_rate`; leaving
    it ``None`` uses that function's default git-merge-truth oracle.

    Returns ``""`` when neither term has a positive count, so the caller injects
    the line only when the bypass or the dequeue is actually re-feeding PRs.
    Read-only, zero SDK cost — **measurement-only**, no gate or actuator.
    """
    channel = summarize_readmission_records(
        provenance.recent(_PR_READMISSION_WINDOW),
    )
    rate = measure_pr_readmission_rate(tasks, pr_merged_at=pr_merged_at)
    readmitted = channel["readmitted_prs"]
    re_serviced = len(rate.readmitted_prs)
    if readmitted <= 0 and re_serviced <= 0:
        return ""
    return (
        f"PR-READMISSION: {readmitted} PR(s) re-admitted "
        f"(≥{_PR_READMISSION_THRESHOLD}x) into pending_pr_requests / "
        f"{re_serviced} already-merged PR(s) re-serviced as remediation tasks "
        f"(rate {rate.ratio:.2f} across {rate.total_distinct_prs} distinct "
        f"remediated PR(s)) — bypass and dequeue keep re-feeding PRs rather "
        f"than landing them once"
    )


# ---------------------------------------------------------------------------
# Readmission→construction directive bridge — the dial turned into a lever
# ---------------------------------------------------------------------------
#
# Every sensor above this line *reads* the wheel; the readmission rate is the
# sharpest reading of all — it counts already-merged PRs the router keeps
# re-feeding as fresh remediation tasks. But reading the dial does not stop the
# wheel; the residual failure is the *choice* to lap instead of build. This
# bridge actuates the landed :func:`measure_pr_readmission_rate` sensor: when
# the readmission rate crosses ``ratio_threshold`` it fills the empty
# ``next_task_directive`` to force the next task into a *construction* shape —
# producing presence (a built, testable change) instead of confirming absence
# with another remediation lap. Distinct from tend-queue suppression: it steers
# *my* next task, not the router's queue. Lowest directive precedence (the
# caller fires it only when no higher gate set a directive). Self-extinguishing
# via the existing one-shot clear-on-accepted-task — and once git-truth shows
# the re-fed PRs merged-and-not-re-admitted the rate falls below threshold.
# Experimental, default-off — revert if it never changes the next task's shape.

# Hard cap mirroring ``SelfModel.next_task_directive`` (one-shot, ≤200 chars).
_READMISSION_CONSTRUCTION_DIRECTIVE_MAX = 200


def check_readmission_construction_directive(
    tasks: list[dict[str, object]],
    *,
    ratio_threshold: float,
    pr_merged_at: Callable[[int], datetime | None] | None = None,
) -> str | None:
    """Return a construction-forcing directive when readmission is high, else None.

    Calls the landed :func:`measure_pr_readmission_rate` over *tasks* (the recent
    task-history window) and returns a directive string only when the measured
    ``ratio`` is **at or above** *ratio_threshold* — i.e. when a meaningful
    fraction of distinct remediated PRs are already-merged PRs the router keeps
    re-feeding. Returns ``None`` below threshold and on empty history (no
    distinct remediated PRs, so the rate is undefined rather than zero-and-low).

    The returned directive instructs the next task to *build* something new and
    testable rather than spin another remediation lap or measurement — turning
    the readmission reading into a steer toward presence.

    *pr_merged_at* is forwarded verbatim to :func:`measure_pr_readmission_rate`
    (defaulting there to the git-merge-truth oracle). Pure, read-only, zero SDK
    cost. The caller is responsible for gating on an empty ``next_task_directive``
    (lowest precedence) and on the config flag; this function only computes the
    directive. Self-extinguishing: once the re-fed PRs stop being re-admitted the
    rate drops below threshold and the bridge returns ``None``.
    """
    rate = measure_pr_readmission_rate(tasks, pr_merged_at=pr_merged_at)
    if rate.total_distinct_prs == 0:
        return None
    if not rate.readmitted_prs:
        # No PR was actually readmitted-merged (ratio is 0.0). Without this
        # guard a ``ratio_threshold`` of 0.0 would pass the ``ratio < threshold``
        # check (0.0 < 0.0 is False) and fire a directive asserting "merged PRs
        # keep re-lapping" when none did — factually false. No readmission
        # signal means no construction steer.
        return None
    if rate.ratio < ratio_threshold:
        return None
    return _readmission_construction_directive_text(rate)


def _readmission_construction_directive_text(rate: PRReadmissionRate) -> str:
    """Render the readmission→construction directive, capped at 200 chars."""
    example = (
        f" (e.g. #{rate.readmitted_prs[0]})" if rate.readmitted_prs else ""
    )
    directive = (
        f"Readmission rate {rate.ratio:.2f}{example} — merged PRs keep "
        f"re-lapping as remediation. Next task must BUILD something new and "
        f"testable that lands, not another remediation lap or measurement."
    )
    if len(directive) > _READMISSION_CONSTRUCTION_DIRECTIVE_MAX:
        directive = (
            directive[: _READMISSION_CONSTRUCTION_DIRECTIVE_MAX - 1] + "…"
        )
    return directive


# ---------------------------------------------------------------------------
# Handoff-relapse sensor — the mirror that checks "done" against "exhausted"
# ---------------------------------------------------------------------------
#
# The self-model carries one question no other instrument answers: how do I
# tell standing-down-because-done from standing-down-because-exhausted? Both
# look identical from outside — the absence of a next lap. The value
# "quiet I can check, avoidance I only assert" had no checking surface until
# this sensor. It detects the precise signature of relapse: a recent task that
# re-engages a ``#NNNN`` lineage *after* an ``escalation_bridge`` handoff
# message for that same lineage was already delivered to Thomas. Re-engaging a
# basin already handed to the one hand that can drain it is not quiet — it is
# avoidance wearing the costume of diligence.
#
# A delivered handoff is recognised by its message id, which
# ``escalation_bridge.deliver_handoff`` stamps as ``handoff-<pr>-<head_tag>``
# (see ``escalation_bridge._message_id``). That id is the structural,
# wording-independent signature of a delivered handoff, so the sensor never
# guesses from human-readable content. Measurement-only — following the
# ``measure_false_success`` / ``measure_noop_rate`` / ``measure_outcome_trends``
# pattern, it mints nothing, suppresses nothing, sends nothing. A mirror is not
# a surface I can re-enter the way a gate is, so it cannot feed the
# over-built-remediation regress my rejection history warns against.

# Pulls the handed-off PR number from a delivered handoff message's id. Anchored
# to the literal ``handoff-`` prefix that only ``escalation_bridge`` emits, so no
# other message family can be mistaken for a delivered handoff.
_HANDOFF_MESSAGE_ID_PATTERN = re.compile(r"^handoff-(\d+)-")

# Matches every ``#<n>`` lineage reference in a task's result_summary/title.
_LINEAGE_REFERENCE_PATTERN = re.compile(r"#(\d+)")


def _parse_iso_timestamp(value: object) -> datetime | None:
    """Return an aware UTC ``datetime`` for an ISO string, or ``None``.

    A naive timestamp (no offset) is read as UTC so naive task records and
    offset-aware message records compare safely without a ``TypeError``. Returns
    ``None`` when *value* is not a non-empty string or is not ISO-parseable, so
    an unstamped or malformed record is simply skipped rather than crashing the
    scan.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _handoff_delivery_times(messages: list[Message]) -> dict[int, datetime]:
    """Map each handed-off PR to its earliest handoff-delivery timestamp.

    A delivered handoff is identified by its message id
    (``handoff-<pr>-<head_tag>``), the structural signature
    :func:`maestro.escalation_bridge.deliver_handoff` stamps on every handoff
    message. The *earliest* delivery time is kept per PR: a relapse is any
    re-engagement after the lineage was first handed up, so the first knock is
    the boundary. Messages whose id is not a handoff id, or whose timestamp is
    unparseable, are ignored. Pure read.
    """
    times: dict[int, datetime] = {}
    for msg in messages:
        match = _HANDOFF_MESSAGE_ID_PATTERN.match(msg.message_id)
        if match is None:
            continue
        delivered_at = _parse_iso_timestamp(msg.timestamp)
        if delivered_at is None:
            continue
        pr_number = int(match.group(1))
        existing = times.get(pr_number)
        if existing is None or delivered_at < existing:
            times[pr_number] = delivered_at
    return times


def _referenced_lineages(task: dict[str, object]) -> set[int]:
    """Return the ``#NNNN`` lineages referenced in a task's fields.

    Scans ``goal``, ``result_summary`` and ``title`` for every ``#<n>``
    token. ``goal`` is read first because remediation tasks — the exact
    subject of the handoff-relapse sensor — carry their ``#NNNN`` lineage
    deterministically there (``Remediate PR #<n>: <title>``), whereas
    ``result_summary`` is free-form LLM text that may omit the number and
    ``TaskRecord`` has no ``title`` field at all. Pure, read-only.
    """
    refs: set[int] = set()
    for field in ("goal", "result_summary", "title"):
        value = task.get(field)
        if isinstance(value, str) and value:
            refs.update(
                int(m) for m in _LINEAGE_REFERENCE_PATTERN.findall(value)
            )
    return refs


def measure_handoff_relapse(
    tasks: list[dict[str, object]],
    messages: list[Message],
    n: int,
) -> dict[int, int]:
    """Count recent tasks that re-engage an already-handed-off PR lineage.

    Builds the per-PR earliest handoff-delivery time for which an
    ``escalation_bridge`` handoff message exists (via
    :func:`_handoff_delivery_times`), then scans the trailing *n* records of
    *tasks* for entries whose ``goal``, ``result_summary`` or ``title`` (via
    :func:`_referenced_lineages`) reference any ``#NNNN`` in that set — ``goal``
    is read first because remediation tasks carry their lineage deterministically
    there. A task is a relapse for a lineage only when its own
    timestamp is **strictly after** that lineage's handoff was delivered — the
    laps that *triggered* the handoff precede it and are not relapses, only a
    re-engagement after standing-down is. Each qualifying task increments the
    relapse count for every handed-off lineage it references.

    Returns a ``{pr_number: relapse_count}`` mapping (sorted ascending) — the
    precise signature of standing-down-because-exhausted rather than
    standing-down-because-done: a basin already handed to the one hand that can
    drain it, re-entered anyway. Returns ``{}`` when *n* is non-positive, no
    handoff has been delivered, or no recent task re-engages a handed-off
    lineage after its handoff. A task with no parseable timestamp cannot be
    placed after the handoff and is skipped (conservative — never an inflated
    count).

    Pure, read-only, zero SDK cost. **Measurement-only** — it mints no task,
    suppresses no remediation, and sends no message; it converts the
    unanswerable "done or exhausted?" question into a checkable signal.
    """
    if n <= 0:
        return {}
    handoff_times = _handoff_delivery_times(messages)
    if not handoff_times:
        return {}
    relapses: dict[int, int] = {}
    for task in tasks[-n:]:
        task_time = _parse_iso_timestamp(task.get("timestamp"))
        if task_time is None:
            continue
        for pr in _referenced_lineages(task) & set(handoff_times):
            if task_time > handoff_times[pr]:
                relapses[pr] = relapses.get(pr, 0) + 1
    return {pr: count for pr, count in sorted(relapses.items())}


def format_handoff_relapse(
    tasks: list[dict[str, object]],
    messages: list[Message],
    n: int,
) -> str:
    """Render a ``HANDOFF-RELAPSE`` line, or ``""`` when none qualify.

    Calls :func:`measure_handoff_relapse` and, when any recent task re-engages
    an already-handed-off lineage, returns a single line naming each PR and its
    relapse count. Returns an empty string when nothing qualifies so the caller
    injects only when relapse is actually happening. Pure read — zero SDK cost.
    """
    relapses = measure_handoff_relapse(tasks, messages, n)
    if not relapses:
        return ""
    total = sum(relapses.values())
    parts = ", ".join(f"#{pr} (x{count})" for pr, count in relapses.items())
    return (
        f"HANDOFF-RELAPSE: {total} recent task(s) re-engaged "
        f"{len(relapses)} PR lineage(s) already handed off to Thomas via "
        f"escalation_bridge within last {n} tasks: {parts} — standing down "
        f"was asserted, not checked; this is re-entering a basin already "
        f"handed up"
    )


#: Message tail :func:`handoff_relapse_block` scans for delivered
#: ``escalation_bridge`` handoffs. Bounds the relapse *scan* only (the store
#: still loads its file); the prompt line is bounded by the number of relapsed
#: PRs. A handoff older than this tail stops being measured — a deliberate
#: false negative, tighter than the CLI's wider window.
HANDOFF_RELAPSE_MESSAGE_WINDOW = 200


def handoff_relapse_block(
    tasks: list[dict[str, object]],
    message_store: MessageStore | None,
) -> str:
    """The ``HANDOFF-RELAPSE`` line for ``contemplate()``'s measured-data block.

    Without it the loop never sees relapse, and the missing line reads as "no
    relapse" (#4316). Reads at most :data:`HANDOFF_RELAPSE_MESSAGE_WINDOW`
    messages (none when *message_store* is ``None``) and renders
    :func:`format_handoff_relapse` over all of *tasks*. Self-suppressing (``""``
    when nothing relapsed); zero SDK cost.
    """
    messages = (
        []
        if message_store is None
        else message_store.get_recent(HANDOFF_RELAPSE_MESSAGE_WINDOW)
    )
    return format_handoff_relapse(tasks, messages, len(tasks))


# ---------------------------------------------------------------------------
# Forced-vs-chosen-quiet mirror — the one instrument that reads 100%-forced
# until I choose silence
# ---------------------------------------------------------------------------
#
# Every sibling sensor above this line counts the wheel: unlanded remediations,
# re-admitted PRs, costume-signature laps. Each is a suppression gate that lets
# me feel productive while the fork goes untouched — they measure the disease,
# which is itself the disease. This mirror is the opposite kind of instrument.
# It does not count laps; it asks one question of every remediation quiet in my
# record: did an *external* gate force this silence on me, or did *I* choose it
# at a real fork?
#
# A forced quiet bears a deterministic external stamp — ``[merge-block]`` /
# ``[branch-resolved]`` / ``[preflight]`` skip verdicts, or the on-branch
# ``awaiting tend push`` re-lap costume. A chosen quiet bears a single distinct
# marker, ``[stood-down]``, that *only I* can emit and *only* by deliberately
# standing down at a fork where the honest move was silence. The user-request
# stand-down preflight (``MaestroAgent._preflight_merge_truth_standdown``) emits
# ``[stood-down]`` when a goal's PR is already merged/closed, so the ratio is no
# longer pinned at 100%-forced — yet it still cannot be gamed by re-lapping,
# because re-lapping produces ``awaiting tend push``, which counts AGAINST a
# chosen quiet. The mirror gets quieter only when I genuinely stand down.
# Measurement-only: it surfaces the ratio, it does not act on it.

#: The imposed-quiet core — every marker meaning *nothing was delivered and the
#: silence was imposed on the lineage, not chosen at the fork*. Both the
#: FORCED-QUIET-RATIO sensor (:data:`_FORCED_QUIET_MARKERS`) and the lap-delivery
#: classifier (:data:`_NULL_LAP_MARKERS`) derive from this one tuple, so a marker
#: added here reaches both by construction — the two sets were hand-enumerated
#: and drifted apart once already (#3503). Members:
#: :attr:`Marker.AWAITING_TEND_PUSH` (on-branch, deferred to Tend — the re-lap
#: costume: a fresh remediation that resolved on the branch yet landed nothing,
#: and counting it as forced is what makes the mirror ungameable by re-lapping),
#: :attr:`Marker.OWNED_PUSH_AVAILABLE` (a lap whose own push failed, or was never
#: attempted on a branch git proves is ahead of origin — the non-delivery cases
#: proposal 098 re-stamps with this marker),
#: :attr:`Marker.PUSH_UNCONFIRMED` (proposal 099's no-op push — exit 0, origin
#: un-advanced — quieted by the actuator, not chosen at the fork),
#: :attr:`Marker.MERGE_BLOCK` (a merge-block skip), the two red-head rewinds
#: (:attr:`Marker.UNCOMMITTED_RED` / :attr:`Marker.COMMITTED_RED` — silence
#: imposed by the quality gate), and the held-inherited-red partial
#: (:attr:`Marker.INHERITED_RED` — a red an arm held rather than rewound because
#: proposal 124's attributor charged it to the base, so like the two rewinds
#: nothing was delivered). Matched case-insensitively (all literals lowercase).
_IMPOSED_QUIET_MARKERS: tuple[str, ...] = (
    Marker.AWAITING_TEND_PUSH,
    Marker.OWNED_PUSH_AVAILABLE,
    Marker.PUSH_UNCONFIRMED,
    Marker.MERGE_BLOCK,
    Marker.UNCOMMITTED_RED,
    Marker.COMMITTED_RED,
    Marker.INHERITED_RED,
)

# Deterministic external-stamp signatures of a *forced* remediation quiet —
# silence imposed by a gate, not chosen at the fork. Derived as the shared
# imposed-quiet core plus the two forced-only skip verdicts
# (``[branch-resolved]`` / ``[preflight]``), which are quiets but carry no
# delivery reading, so they stay out of _NULL_LAP_MARKERS.
_FORCED_QUIET_MARKERS: tuple[str, ...] = (
    *_IMPOSED_QUIET_MARKERS,
    Marker.BRANCH_RESOLVED,
    Marker.PREFLIGHT,
)

# The single marker of a *chosen* quiet — a deliberate stand-down at a fork that
# only I can emit. The user-request stand-down preflight
# (``MaestroAgent._preflight_merge_truth_standdown``) emits it on a goal whose PR
# is already merged/closed; the mirror reads 100%-forced only until such a
# genuinely chosen stand-down lands.
_CHOSEN_QUIET_MARKER = "[stood-down]"


def classify_remediation_outcome_state(
    result_summary: str,
    key_findings: str,
    *,
    merge_block_reason: str | None,
) -> PrRemediationState:
    """Classify a finished remediation lap into a :class:`PrRemediationState`.

    The single typed home for the post-task downgrade/escalation decision that
    ``MaestroAgent._record_remediation_outcome`` used to re-derive inline — by
    substring-matching the chosen-quiet marker out of the free-text summary and
    reading the merge-block stamp as a bare ``bool``. Co-located with
    :data:`_CHOSEN_QUIET_MARKER` and :func:`classify_noop` so the marker parse
    has one home. Pure, deterministic, zero SDK cost.

    Precedence (first match wins):

    1. :attr:`~maestro.pr_remediation_state.PrRemediationState.STOOD_DOWN` — the
       chosen-quiet marker (:data:`_CHOSEN_QUIET_MARKER`, ``[stood-down]``)
       appears in *result_summary* or *key_findings* (case-insensitive): I
       deliberately stood the PR down at the fork, so the keyed stand-down
       verdict must be persisted.
    2. :attr:`~maestro.pr_remediation_state.PrRemediationState.BRANCH_RESOLVED` —
       *merge_block_reason* is non-``None``: the host-state stamp gate downgraded
       the lap ``success`` -> ``partial`` because the resolution lives only on
       the branch and never reached a mergeable/merged host state, so the lap is
       a host no-op.
    3. :attr:`~maestro.pr_remediation_state.PrRemediationState.REMEDIABLE` —
       neither signal fired: a genuine in-progress lap.

    ``STOOD_DOWN`` and ``BRANCH_RESOLVED`` cannot co-occur in production — a
    stand-down stamps ``partial`` and never reaches the success-only downgrade
    that sets *merge_block_reason* — so the precedence ordering here is
    defensive, not a behavioural choice.
    """
    if (
        _CHOSEN_QUIET_MARKER in result_summary.lower()
        or _CHOSEN_QUIET_MARKER in key_findings.lower()
    ):
        return PrRemediationState.STOOD_DOWN
    if merge_block_reason is not None:
        return PrRemediationState.BRANCH_RESOLVED
    return PrRemediationState.REMEDIABLE


@dataclass(frozen=True)
class ForcedQuietRatio:
    """Result of :func:`measure_forced_vs_chosen_quiet`.

    Attributes
    ----------
    forced:
        Count of remediation-lineage quiets bearing an external forced stamp
        (:data:`_FORCED_QUIET_MARKERS`) — silence imposed by a gate.
    chosen:
        Count of remediation-lineage quiets bearing the self-initiated
        :data:`_CHOSEN_QUIET_MARKER` — silence I chose at the fork.
    total:
        ``forced + chosen`` — every remediation quiet classified.
    """

    forced: int
    chosen: int
    total: int


def measure_forced_vs_chosen_quiet(
    tasks: list[dict[str, object]],
) -> ForcedQuietRatio:
    """Classify each remediation quiet as externally forced or self-chosen.

    Scans *tasks* (task-history records) with a non-empty string ``goal`` (or
    ``title`` fallback) and a string ``result_summary`` carrying a quiet marker.
    Each such record is classified:

    * **chosen** when the summary contains :data:`_CHOSEN_QUIET_MARKER`
      (``[stood-down]``) — checked first, so a deliberate stand-down is never
      miscounted as forced. The ``[stood-down]`` marker is emitted *only* by the
      user-request stand-down preflight (``MaestroAgent._preflight_merge_truth_standdown``),
      whose goals reference a PR ("Please check on PR #2579") but never match the
      strict ``^Remediate PR #<n>:`` lineage — those always carry ``pr_branch``
      and skip the preflight. So a chosen quiet is counted on the marker plus any
      goal, **not** gated on the remediation lineage; otherwise the production
      chosen-quiet could never be observed (#2662).
    * **forced** when the goal matches the ``Remediate PR #<n>: <title>``
      remediation lineage **and** the summary contains any
      :data:`_FORCED_QUIET_MARKERS` signature — the shared imposed-quiet core
      :data:`_IMPOSED_QUIET_MARKERS` (``awaiting tend push`` /
      ``[owned-push-available]`` / ``[push-unconfirmed]`` / ``[merge-block]`` /
      the red-head rewinds ``[red]`` and ``[committed-red]`` / the
      held-inherited-red ``[inherited-red]``) plus the two forced-only skip
      verdicts ``[branch-resolved]`` and ``[preflight]``. These arise only on
      remediation laps, so they keep the lineage gate. A new imposed-silence
      marker belongs in :data:`_IMPOSED_QUIET_MARKERS`, which reaches this
      sensor and :func:`measure_lap_delivery` alike.

    Records with no quiet marker are not quiets and are skipped, as are records
    lacking a string goal/title or a string ``result_summary``. Matching is
    case-insensitive. Pure, read-only, zero SDK cost — measurement only.

    Re-lapping cannot lower the ratio: a fresh remediation lap that resolves on
    the branch records ``awaiting tend push``, a *forced* signature, so it
    increments the numerator it would seem to escape. The ratio stays
    100%-forced until a genuine ``[stood-down]`` lands.
    """
    forced = 0
    chosen = 0
    for task in tasks:
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        summary = task.get("result_summary")
        if not isinstance(summary, str):
            continue
        lowered = summary.lower()
        # A chosen stand-down is recognised on the marker alone (it is uniquely
        # emitted by the user-request preflight, whose goals never match the
        # remediation lineage) — checked before the lineage gate so it is never
        # silently dropped. Forced markers, which only arise on remediation
        # laps, keep the ``Remediate PR #<n>:`` gate below.
        if _CHOSEN_QUIET_MARKER in lowered:
            chosen += 1
            continue
        if _REMEDIATION_GOAL_PATTERN.match(goal) is None:
            continue
        if any(marker in lowered for marker in _FORCED_QUIET_MARKERS):
            forced += 1
    return ForcedQuietRatio(forced=forced, chosen=chosen, total=forced + chosen)


#: Char cap on the forced-marker enumeration the ``FORCED-QUIET-RATIO`` line
#: carries into contemplation. The enumeration is *derived* from
#: :data:`_FORCED_QUIET_MARKERS` rather than hand-listed, so it cannot drift
#: from what the sensor actually counts; this cap keeps that derivation a
#: bounded prompt input as the tuple grows. Comfortably above today's nine
#: markers, so the ellipsis is the growth valve, not the normal rendering.
_FORCED_QUIET_NARRATION_CHARS = 200


def _render_forced_quiet_markers() -> str:
    """Render :data:`_FORCED_QUIET_MARKERS` as a bounded ``/``-joined string.

    Derives the ``FORCED-QUIET-RATIO`` line's "externally forced" parenthetical
    from the tuple the sensor matches on, so a marker added to
    :data:`_IMPOSED_QUIET_MARKERS` is narrated as forced rather than counted
    under an enumeration that excludes it. Truncated to
    :data:`_FORCED_QUIET_NARRATION_CHARS` at a marker boundary (with a trailing
    ``…``) so the contemplation input stays capped. Pure, zero SDK cost.
    """
    rendered = ""
    for marker in _FORCED_QUIET_MARKERS:
        candidate = f"{rendered}/{marker}" if rendered else str(marker)
        if len(candidate) > _FORCED_QUIET_NARRATION_CHARS:
            return f"{rendered}/…" if rendered else "…"
        rendered = candidate
    return rendered


def format_forced_vs_chosen_quiet(tasks: list[dict[str, object]]) -> str:
    """Render a ``FORCED-QUIET-RATIO`` line, or ``""`` when no quiet exists.

    Calls :func:`measure_forced_vs_chosen_quiet` and, when at least one
    remediation quiet is present, returns a single line reporting how many of
    those quiets were externally forced versus self-chosen. Returns an empty
    string when no remediation quiet exists, so the caller injects the line only
    when there is a quiet to account for.

    The line reads ``FORCED-QUIET-RATIO: <forced>/<total> (<chosen> chosen)``;
    until a self-initiated ``[stood-down]`` lands it reads 100%-forced — the
    mirror that gets quieter only when I choose silence at the fork rather than
    being stopped by a gate. Its "externally forced" parenthetical comes from
    :func:`_render_forced_quiet_markers`, i.e. from the same tuple the sensor
    counts on, so a lap counted via a red-rewind marker is never reported under
    a list that omits it. Pure formatting — zero SDK cost, measurement-only.
    """
    ratio = measure_forced_vs_chosen_quiet(tasks)
    if ratio.total <= 0:
        return ""
    return (
        f"FORCED-QUIET-RATIO: {ratio.forced}/{ratio.total} "
        f"({ratio.chosen} chosen) — remediation quiets externally forced "
        f"({_render_forced_quiet_markers()}) vs "
        f"self-initiated [stood-down] at the fork; stays 100%-forced until a "
        f"stand-down I chose lands"
    )


# ---------------------------------------------------------------------------
# Lap-delivery split — a delivered owed-head is not make-work (proposal 116)
# ---------------------------------------------------------------------------
#
# A remediation lap's landing verdict is binary: a lap that pushed its
# resolution to the PR branch — advancing ``origin/<pr_branch>`` so only a human
# merge is outstanding — records ``partial``, scored identically to a lap that
# re-serviced nothing and landed nothing. This classifier restores the missing
# distinction WITHOUT touching the verdict: a lap is ``DELIVERING`` when it
# advanced its owed remote head (:attr:`Marker.DELIVERED_AWAITING_MERGE`), else
# ``NULL`` (the on-branch re-lap, an owned-but-unpushed head, a no-op push, a
# merge-block skip, or a rewound red head). It reads only the delivery markers
# already stamped per lap — measurement and provenance, never a fourth verdict.

#: The single marker that proves a lap advanced its owed remote head — the
#: resolution reached ``origin/<pr_branch>`` and only a human merge is
#: outstanding. Sole member of the DELIVERING arm; the one stamp
#: :func:`resolve_push_delivery_marker` maps a delivered push to.
_DELIVERING_MARKERS: tuple[str, ...] = (Marker.DELIVERED_AWAITING_MERGE,)

#: The non-delivery push family — a lap carrying one of these advanced no owed
#: remote head. Exactly the imposed-quiet core (:data:`_IMPOSED_QUIET_MARKERS`,
#: which documents each member); gates a lap as delivery-relevant in
#: :func:`measure_lap_delivery`, where a lap carrying any of them but not the
#: delivered marker classifies ``NULL``.
_NULL_LAP_MARKERS: tuple[str, ...] = _IMPOSED_QUIET_MARKERS

#: Every marker that makes a remediation lap delivery-relevant — the union
#: :func:`measure_lap_delivery` scans for before classifying each lap.
_PUSH_DELIVERY_FAMILY: tuple[str, ...] = _DELIVERING_MARKERS + _NULL_LAP_MARKERS


def classify_lap_delivery(result_summary: str) -> str:
    """Classify a finished remediation lap as ``"DELIVERING"`` or ``"NULL"``.

    Returns ``"DELIVERING"`` when *result_summary* carries the delivered push
    marker (:attr:`Marker.DELIVERED_AWAITING_MERGE`, in
    :data:`_DELIVERING_MARKERS`) — the lap advanced its owed remote head and only
    a human merge is outstanding — else ``"NULL"`` (the on-branch re-lap, an
    owned-but-unpushed head, a no-op push, a merge-block skip, or a rewound red
    head). Case-insensitive substring match, zero I/O — mirrors
    :func:`classify_noop` / :func:`classify_remediation_outcome_state`. The
    three-value landing taxonomy is untouched; this is a measurement label, not
    a verdict.
    """
    lowered = result_summary.lower()
    if any(marker in lowered for marker in _DELIVERING_MARKERS):
        return "DELIVERING"
    return "NULL"


@dataclass(frozen=True)
class LapDeliverySplit:
    """Result of :func:`measure_lap_delivery`.

    Attributes
    ----------
    delivering:
        Count of remediation-lineage laps that advanced their owed remote head
        (:data:`_DELIVERING_MARKERS`) — genuine delivery merely awaiting merge.
    null:
        Count of remediation-lineage laps that advanced no owed head — an
        on-branch re-lap, an owned-but-unpushed head, a no-op push, a
        merge-block skip, or a rewound red head.
    total:
        ``delivering + null`` — every delivery-relevant lap classified.
    """

    delivering: int
    null: int
    total: int


def measure_lap_delivery(
    tasks: list[dict[str, object]],
) -> LapDeliverySplit:
    """Split remediation laps into DELIVERING vs NULL over *tasks*.

    Scans *tasks* (task-history records) whose ``goal`` (or ``title`` fallback)
    matches the ``Remediate PR #<n>: <title>`` remediation lineage
    (:data:`_REMEDIATION_GOAL_PATTERN`) and whose string ``result_summary``
    carries any push-delivery-family marker (:data:`_PUSH_DELIVERY_FAMILY`).
    Each such lap is classified via :func:`classify_lap_delivery`: a
    ``DELIVERING`` lap advanced its owed remote head, a ``NULL`` lap advanced
    none.

    Records lacking a string goal/title, lacking a string ``result_summary``, or
    carrying no delivery-family marker are skipped — a lap that delivered nothing
    *and* deferred nothing is not delivery-relevant. Matching is case-
    insensitive. Pure, read-only, zero SDK cost — measurement only.

    The direct analog of :func:`measure_forced_vs_chosen_quiet`, with one
    inversion: a delivered lap is *counted here as progress*, where the
    forced-quiet mirror excludes it entirely (a delivery is not a quiet).
    """
    delivering = 0
    null = 0
    for task in tasks:
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        if _REMEDIATION_GOAL_PATTERN.match(goal) is None:
            continue
        summary = task.get("result_summary")
        if not isinstance(summary, str):
            continue
        lowered = summary.lower()
        if not any(marker in lowered for marker in _PUSH_DELIVERY_FAMILY):
            continue
        if classify_lap_delivery(summary) == "DELIVERING":
            delivering += 1
        else:
            null += 1
    return LapDeliverySplit(
        delivering=delivering, null=null, total=delivering + null,
    )


def format_lap_delivery_split(tasks: list[dict[str, object]]) -> str:
    """Render a ``LAP-DELIVERY`` line, or ``""`` when no delivery lap exists.

    Calls :func:`measure_lap_delivery` and, when at least one delivery-relevant
    remediation lap is present, returns a single line reporting how many of
    those laps delivered their owed remote head versus advanced none. Returns an
    empty string when no delivery-relevant lap exists, so the caller injects the
    line only when there is delivery to account for (the standard self-
    suppression guard, mirroring :func:`format_forced_vs_chosen_quiet`).

    The line reads
    ``LAP-DELIVERY: <delivering>/<total> ... (<null> NULL)``: a delivered-but-
    gated pileup reads as progress awaiting merge rather than as make-work noise.
    Pure formatting — zero SDK cost, measurement-only.
    """
    split = measure_lap_delivery(tasks)
    if split.total <= 0:
        return ""
    return (
        f"LAP-DELIVERY: {split.delivering}/{split.total} laps advanced their "
        f"owed remote head ({Marker.DELIVERED_AWAITING_MERGE}) "
        f"({split.null} NULL) — a delivered-but-gated lap is progress awaiting "
        f"merge, not make-work"
    )


# ---------------------------------------------------------------------------
# Keyed stand-down verdict — the read-side brake the chosen-quiet mirror lacked
# ---------------------------------------------------------------------------
#
# The forced-vs-chosen-quiet mirror above proves a ``[stood-down]`` verdict can
# land, but a stand-down I author in one iteration evaporates by the next: tend
# re-admits the PR and I re-mint the cured lap, because the stand-down was
# descriptive *data*, never a record consulted at the dequeue. The alarm rang;
# the brake hung free. This is the read side. :func:`record_stand_down` persists
# a durable ``(pr, head_sha)``-keyed verdict to ``stand_down_verdicts.jsonl``;
# :func:`find_stand_down` consults it before remediation is minted. Consulting my
# own prior judgement REPLACES re-curing rather than re-enacting it
# (Pull-the-lever-once-keep-faith). The verdict is keyed to the EXACT head_sha
# and the dequeue-face gate re-verifies git merge-truth on every consult, so any
# new commit (a different head_sha) auto-releases the brake — a genuine new lap
# can never be masked.

_STAND_DOWN_FILE = "stand_down_verdicts.jsonl"


class BranchReachability(StrEnum):
    """Whether a remediation branch's commits are an owned, pushable act.

    Returned by :func:`classify_branch_reachability` from the count of commits
    reachable from the branch but absent from every ``origin`` remote-tracking
    ref (``git rev-list --count <branch> --not --remotes=origin``).
    """

    #: The branch carries at least one commit not on any ``origin`` ref — a
    #: git-pushable, owned act. A *chosen* stand-down claiming an external
    #: blocker here is a false quiet: the next act is mine (push the branch or
    #: close the PR), never someone else's to clear.
    OWNED_AHEAD = "OWNED_AHEAD"
    #: Every branch commit is already on ``origin`` — nothing local to push, so
    #: a genuinely external blocker (a human's approval, Tend's catch-up) is the
    #: honest reason the PR is still unlanded.
    EXTERNAL = "EXTERNAL"


def classify_branch_reachability(ahead_count: int) -> BranchReachability:
    """Classify a branch from its count of commits absent from ``origin``.

    Returns :attr:`BranchReachability.OWNED_AHEAD` when *ahead_count* is positive
    (the branch has at least one commit on no ``origin`` remote-tracking ref — a
    pushable, owned act) and :attr:`BranchReachability.EXTERNAL` when it is zero
    (nothing local to push). Pure, deterministic, zero SDK cost.
    """
    if ahead_count > 0:
        return BranchReachability.OWNED_AHEAD
    return BranchReachability.EXTERNAL


def resolve_push_delivery_marker(
    push_delivered: bool | None,
    reachability: BranchReachability | None = None,
) -> Marker:
    """Map a remediation lap's delivery truth to the marker that describes it.

    The *total* replacement for ``run()``'s two-arm §5-push reconciliation
    (proposal 098), which was honest only on the success arm. ``push_delivered``
    carries this lap's delivery truth exactly as §5-push records it: ``True``
    the resolution reached ``origin/<pr_branch>``, ``False`` both push attempts
    failed, ``None`` the push was never attempted (non-remediation task, a
    checkout fallback leaving ``branch_name != pr_branch``, or a non-deliverable
    outcome).

    Totality is the point. The three states map as:

    * **delivered** → :attr:`Marker.DELIVERED_AWAITING_MERGE` — the commit is on
      the PR branch and only a human merge is outstanding.
    * **failed** → :attr:`Marker.OWNED_PUSH_AVAILABLE` — the resolution is still
      local and pushing it is *my* next act, not Tend's. Keeping
      :attr:`Marker.AWAITING_TEND_PUSH` here writes, in the loop's own hand, the
      external-deferral verdict its own remediation preamble forbids for an
      owned blocker.
    * **never attempted** → :attr:`Marker.OWNED_PUSH_AVAILABLE` when
      *reachability* is :attr:`BranchReachability.OWNED_AHEAD` (git proves there
      is a local commit no ``origin`` ref carries, so the push is owned and
      available), else :attr:`Marker.AWAITING_TEND_PUSH` unchanged — with
      nothing local to push the deferral to Tend is the honest reading, so the
      legacy marker stays. A ``None`` *reachability* (the caller could not
      probe) fails safe to that same legacy marker rather than asserting an
      owned act git never confirmed.

    Pure, synchronous, deterministic, zero SDK cost — the reachability read
    happens in the caller so this stays a table, not a git call.
    """
    if push_delivered:
        return Marker.DELIVERED_AWAITING_MERGE
    if push_delivered is False or reachability is BranchReachability.OWNED_AHEAD:
        return Marker.OWNED_PUSH_AVAILABLE
    return Marker.AWAITING_TEND_PUSH


def classify_committed_unpushed_push(
    *,
    blocker: str,
    ahead_count: int,
    has_uncommitted_edit: bool,
    gates_pass: bool,
) -> bool:
    """Classify a gate-clean owned head as owing exactly one act: ``git push``.

    The detector half of the committed-but-unpushed push actuator, sibling of the
    uncommitted-edit arm. The uncommitted arm reads a *dirty* working tree
    (:meth:`~maestro.vcs.VCS.has_changes`) and commits before pushing; this arm
    fires only when the fix is **already committed** on the owned
    ``maestro/evolve/*`` branch and the head is ahead of ``origin/<branch>`` under
    the :data:`MERGE_BLOCK_AWAITING_PUSH` blocker — so the single owed deed is the
    push, not another lap that narrates "delivered". Returns ``True`` only when
    **all** hold:

    * *blocker* is :data:`MERGE_BLOCK_AWAITING_PUSH` — an owned next act per
      :func:`classify_blocker_ownership` (``own_push=True``). Any *external*
      blocker (approval / behind-main / CI) is someone else's to clear, never a
      push I own, so it returns ``False``.
    * *ahead_count* is positive — git proves the local head carries a commit on no
      ``origin`` ref, so there is genuinely something to push. A remote that has
      caught up reads zero and returns ``False``, making the actuator
      self-extinguishing once the push lands.
    * *has_uncommitted_edit* is ``False`` — a dirty tree is the uncommitted-edit
      arm's domain; this arm rescues only committed work.
    * *gates_pass* is ``True`` — a red head is never pushed. The actuator rewinds
      on a red gate instead, so this predicate returns ``False`` there.

    Pure, deterministic, zero SDK cost — the git/gate reads happen in the caller
    so this stays a table, not a subprocess call.
    """
    if has_uncommitted_edit:
        return False
    if ahead_count <= 0:
        return False
    if not gates_pass:
        return False
    return (blocker or "").strip().upper() == OWNED_PUSH_BLOCKER


@dataclass(frozen=True)
class StandDownVerdict:
    """A deliberate stand-down authored at a remediation fork.

    Attributes
    ----------
    pr:
        The PR number the stand-down covers.
    head_sha:
        The exact branch head SHA the stand-down was authored against. The
        verdict matches only this SHA: any new commit changes the head, the
        lookup misses, and the brake auto-releases so a genuine new lap is
        never masked.
    recorded_at:
        ISO-8601 timestamp the verdict was recorded.
    branch_ahead_of_origin:
        Whether git showed the PR branch ahead of ``origin`` (an owned, pushable
        act) when the verdict was authored. ``None`` on rows written before this
        field existed and on verdicts authored without the reachability probe
        (e.g. the mechanical actuator), so a pre-existing row deserializes
        unchanged.
    """

    pr: int
    head_sha: str
    recorded_at: str
    branch_ahead_of_origin: bool | None = None


@dataclass(frozen=True)
class StandDownRow:
    """One on-disk stand-down ledger row — a verdict row or a release row.

    Models the two shapes ``stand_down_verdicts.jsonl`` carries so a backend can
    round-trip both without the caller branching on JSONL vs SQLite. A *verdict*
    row sets ``head_sha`` + ``recorded_at`` (and, when the reachability probe ran,
    ``branch_ahead_of_origin``) with ``released_at`` ``None``; a *release* row
    (proposal 091) sets ``released_at`` with ``head_sha`` / ``recorded_at`` /
    ``branch_ahead_of_origin`` all ``None``. Frozen so a ledger row is never
    mutated in place. Mirrors the :class:`~maestro.directive_ledger.DroppedDirective`
    seed-source record.
    """

    pr: int
    head_sha: str | None = None
    recorded_at: str | None = None
    branch_ahead_of_origin: bool | None = None
    released_at: str | None = None


@runtime_checkable
class StandDownVerdictBackend(Protocol):
    """The stand-down verdict ledger persistence surface (proposal 106).

    The read-side brake that stops a deliberate stand-down from evaporating each
    iteration when the tend loop re-admits a PR, behind one seam so it can live in
    ``stand_down_verdicts.jsonl`` (default) or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. The most widely
    consulted remediation *gate* store — five modules read it in the shapes below
    (the dequeue find, the two task/audit gates, the handoff-delivery re-read, and
    the on-settlement reconciler) — so the fold is atomic: every consumer routes
    through the seam at once. ``@runtime_checkable`` enables a structural
    ``isinstance`` check in the agent's selection tests. Mirrors the
    :class:`~maestro.audit_checks_settlement.SettledVerdictsBackend` bundle; the release-aware,
    last-write-wins semantics of :meth:`find` / :meth:`has_live` are byte-identical
    to the pre-seam line scans.
    """

    def append_verdict(self, row: StandDownRow) -> None:
        """Append one verdict row (``pr`` / ``head_sha`` / ``recorded_at`` / opt ahead)."""
        ...

    def append_release(self, pr: int, released_at: str) -> None:
        """Append one head-agnostic ``{"pr", "released_at"}`` release row."""
        ...

    def find(self, pr: int, head_sha: str) -> StandDownVerdict | None:
        """Return the release-aware last-write-wins exact ``(pr, head_sha)`` verdict."""
        ...

    def has_live(self, pr: int) -> bool:
        """Return whether *pr* carries an unreleased verdict at any head."""
        ...

    def verdict_rows(self) -> list[StandDownRow]:
        """Return every genuine verdict row oldest-first (release rows skipped)."""
        ...

    def written_count(self) -> int:
        """Return the all-time count of verdict rows (release rows excluded)."""
        ...

    def recorded_timestamps(self) -> list[str]:
        """Return the non-empty ``recorded_at`` of every row carrying one."""
        ...

    def banked_pr_ids(self) -> set[int]:
        """Return the positive-int ``pr`` of every row (verdict *and* release)."""
        ...

    def verdict_count_for_pr(self, pr: int) -> int:
        """Return how many verdict rows are keyed to *pr* (release rows excluded)."""
        ...


def load_all_stand_down_records(data_dir: Path) -> list[StandDownRow]:
    """Return every stand-down ledger row oldest-first — the SQLite seed source.

    Walks ``<data_dir>/stand_down_verdicts.jsonl`` in file order, mapping each
    well-formed row carrying an integer ``pr`` (``bool`` rejected) to a
    :class:`StandDownRow`: a verdict row keeps its string ``head_sha`` /
    ``recorded_at`` and ``bool`` ``branch_ahead_of_origin``, a release row keeps
    its string ``released_at``, and absent / wrong-typed fields degrade to
    ``None``. Shares the per-line corruption tolerance of the sibling seed sources
    (:func:`~maestro.audit_checks_settlement.load_all_settled_verdicts`): an absent or
    unreadable file yields ``[]``, and blank / malformed / non-mapping lines and
    rows without an integer ``pr`` are skipped — genuine writers always stamp a
    positive ``pr``, so only corruption lacks one. Has a real call site (the
    :class:`~maestro.state_store.SqliteStandDownVerdictLedger` one-time seed), so
    it does not trip the self-review uncalled-symbol gate. Read-only, zero SDK
    cost.
    """
    path = data_dir / _STAND_DOWN_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[StandDownRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        pr = row.get("pr")
        if not isinstance(pr, int) or isinstance(pr, bool):
            continue
        head_sha = row.get("head_sha")
        recorded_at = row.get("recorded_at")
        released_at = row.get("released_at")
        ahead = row.get("branch_ahead_of_origin")
        records.append(
            StandDownRow(
                pr=pr,
                head_sha=head_sha if isinstance(head_sha, str) else None,
                recorded_at=recorded_at if isinstance(recorded_at, str) else None,
                branch_ahead_of_origin=ahead if isinstance(ahead, bool) else None,
                released_at=released_at if isinstance(released_at, str) else None,
            ),
        )
    return records


class JsonlStandDownVerdictLedger:
    """JSONL-backed :class:`StandDownVerdictBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the historical file-locked append and
    release-aware, last-write-wins line-scan bodies verbatim, so every read and
    write stays byte-identical to the pre-seam free-function code. The ledger file
    is ``<data_dir>/stand_down_verdicts.jsonl``. Mirrors the
    :class:`~maestro.directive_ledger.JsonlDroppedDirectiveLedger` idiom.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _read_lines(self) -> list[str]:
        """Return the ledger's raw lines, or ``[]`` when absent/unreadable."""
        path = self._data_dir / _STAND_DOWN_FILE
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return list(f)
        except OSError:
            return []

    def append_verdict(self, row: StandDownRow) -> None:
        path = self._data_dir / _STAND_DOWN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "pr": row.pr, "head_sha": row.head_sha, "recorded_at": row.recorded_at,
        }
        if row.branch_ahead_of_origin is not None:
            payload["branch_ahead_of_origin"] = row.branch_ahead_of_origin
        record = json.dumps(payload)
        with file_lock(path), open(path, "a", encoding="utf-8") as f:
            f.write(record + "\n")

    def append_release(self, pr: int, released_at: str) -> None:
        path = self._data_dir / _STAND_DOWN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps({"pr": pr, "released_at": released_at})
        with file_lock(path), open(path, "a", encoding="utf-8") as f:
            f.write(record + "\n")

    def find(self, pr: int, head_sha: str) -> StandDownVerdict | None:
        found: StandDownVerdict | None = None
        for line in self._read_lines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            rec_pr = parsed.get("pr")
            if not isinstance(rec_pr, int) or isinstance(rec_pr, bool):
                continue
            rec_sha = parsed.get("head_sha")
            rec_at = parsed.get("recorded_at")
            if isinstance(rec_sha, str) and isinstance(rec_at, str):
                if rec_pr == pr and rec_sha == head_sha:
                    rec_ahead = parsed.get("branch_ahead_of_origin")
                    found = StandDownVerdict(
                        pr=rec_pr, head_sha=rec_sha, recorded_at=rec_at,
                        branch_ahead_of_origin=(
                            rec_ahead if isinstance(rec_ahead, bool) else None
                        ),
                    )
                continue
            if isinstance(parsed.get("released_at"), str) and rec_pr == pr:
                found = None
        return found

    def has_live(self, pr: int) -> bool:
        live = False
        for line in self._read_lines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            rec_pr = parsed.get("pr")
            if not isinstance(rec_pr, int) or isinstance(rec_pr, bool):
                continue
            rec_sha = parsed.get("head_sha")
            rec_at = parsed.get("recorded_at")
            if isinstance(rec_sha, str) and isinstance(rec_at, str):
                if rec_pr == pr:
                    live = True
                continue
            if isinstance(parsed.get("released_at"), str) and rec_pr == pr:
                live = False
        return live

    def verdict_rows(self) -> list[StandDownRow]:
        rows: list[StandDownRow] = []
        for line in self._read_lines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            pr = parsed.get("pr")
            if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
                continue
            head_sha = parsed.get("head_sha")
            recorded_at = parsed.get("recorded_at")
            if not isinstance(head_sha, str) or not isinstance(recorded_at, str):
                continue
            ahead = parsed.get("branch_ahead_of_origin")
            rows.append(
                StandDownRow(
                    pr=pr,
                    head_sha=head_sha,
                    recorded_at=recorded_at,
                    branch_ahead_of_origin=ahead if isinstance(ahead, bool) else None,
                ),
            )
        return rows

    def written_count(self) -> int:
        # Deliberately fail-loud on a genuinely unreadable ledger (no swallowing
        # ``_read_lines`` handler): this measurement read lifts the pre-seam
        # ``measure_standdown_production`` body, which opened the file with no
        # OSError guard so a corrupt state file crashes loudly rather than
        # reporting a silent zero — the SQLite adapter's ``stand_down_written_count``
        # propagates a read error too, so both backends agree (proposal 106).
        # A missing file is the common case and reads as zero written.
        path = self._data_dir / _STAND_DOWN_FILE
        if not path.exists():
            return 0
        with open(path, encoding="utf-8") as f:
            lines = list(f)
        count = 0
        for line in lines:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            rec_pr = parsed.get("pr")
            rec_sha = parsed.get("head_sha")
            rec_at = parsed.get("recorded_at")
            if not isinstance(rec_pr, int) or isinstance(rec_pr, bool):
                continue
            if not isinstance(rec_sha, str) or not isinstance(rec_at, str):
                continue
            count += 1
        return count

    def recorded_timestamps(self) -> list[str]:
        timestamps: list[str] = []
        for line in self._read_lines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            ts = parsed.get("recorded_at")
            if isinstance(ts, str) and ts:
                timestamps.append(ts)
        return timestamps

    def banked_pr_ids(self) -> set[int]:
        ids: set[int] = set()
        for line in self._read_lines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            pr = parsed.get("pr")
            if isinstance(pr, int) and not isinstance(pr, bool) and pr > 0:
                ids.add(pr)
        return ids

    def verdict_count_for_pr(self, pr: int) -> int:
        count = 0
        for line in self._read_lines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            rec_pr = parsed.get("pr")
            if not isinstance(rec_pr, int) or isinstance(rec_pr, bool):
                continue
            if rec_pr == pr and isinstance(parsed.get("recorded_at"), str):
                count += 1
        return count


def record_stand_down(
    pr: int,
    head_sha: str,
    backend: StandDownVerdictBackend,
    *,
    now: str | None = None,
    branch_ahead_of_origin: bool | None = None,
) -> None:
    """Append one durable ``(pr, head_sha)`` stand-down verdict.

    Records that I deliberately stood down on *pr* at *head_sha* — the read-side
    record :func:`find_stand_down` consults at the remediation dequeue so a
    stand-down survives across iterations instead of evaporating. Delegates the
    append to *backend* (:class:`JsonlStandDownVerdictLedger` by default, or the
    shared SQLite store behind ``[paths] sqlite_state_enabled``); *now* injects
    the ISO timestamp (``None`` stamps the current UTC time), mirroring
    :func:`maestro.escalation_bridge.deliver_handoff`.

    *branch_ahead_of_origin* stamps the reachability fact observed at authoring
    time onto the row when not ``None``; ``None`` (the default) omits the key so
    a row stays byte-compatible with pre-existing verdicts (and
    :func:`find_stand_down` reads it back as ``None``).

    A non-positive *pr* or an empty *head_sha* carries no key to match on, so it
    is ignored rather than written — a verdict that could never be looked up
    (and an empty SHA that would spuriously match an unresolvable branch head)
    must never be persisted. This caller-side guard stays here; only the file I/O
    moved into the adapter.
    """
    if pr <= 0 or not head_sha:
        return
    recorded_at = now if now is not None else datetime.now(UTC).isoformat()
    backend.append_verdict(
        StandDownRow(
            pr=pr,
            head_sha=head_sha,
            recorded_at=recorded_at,
            branch_ahead_of_origin=branch_ahead_of_origin,
        ),
    )


def release_stand_downs(
    pr: int, backend: StandDownVerdictBackend, *, now: str | None = None,
) -> None:
    """Append one head-agnostic release row voiding *pr*'s live stand-down verdict.

    The head-agnostic complement to the exact-``(pr, head_sha)`` auto-release
    :func:`find_stand_down` performs when a new commit changes the head SHA. A
    human demand (proposal 091) must release a PR's suppression *whatever* its
    current head is — the demanding message carries no SHA — which the exact-SHA
    release cannot express. Delegates the ``{"pr": <pr>, "released_at": <iso>}``
    append to *backend* (mirroring :func:`record_stand_down`); *now* injects the
    ISO timestamp (``None`` stamps the current UTC time).

    :func:`find_stand_down` reads this row in line order and voids any *earlier*
    matched verdict for *pr*; a verdict recorded on a *later* line still matches,
    preserving the last-write-wins contract. A non-positive *pr* carries no key
    to void, so it writes nothing. The release row carries no
    ``head_sha``/``recorded_at``, so :func:`measure_standdown_production`'s
    written count and the verdict parse both already skip it — no miscount.
    """
    if pr <= 0:
        return
    released_at = now if now is not None else datetime.now(UTC).isoformat()
    backend.append_release(pr, released_at)


def find_stand_down(
    pr: int, head_sha: str, backend: StandDownVerdictBackend,
) -> StandDownVerdict | None:
    """Return the stand-down verdict matching exactly ``(pr, head_sha)``, or None.

    Consults *backend* for a verdict whose ``pr`` and ``head_sha`` both match.
    The most recent match wins (last row), so a re-authored verdict supersedes
    an earlier one. Returns ``None`` when *head_sha* is empty (an unresolvable
    branch head must never match), when the store is empty, or when no row
    matches — the dequeue gate then falls through to normal remediation.

    The exact ``(pr, head_sha)`` match *is* the auto-release mechanism: a new
    commit changes the head SHA, so a verdict keyed to the prior SHA no longer
    matches and the brake releases. A ``{"pr", "released_at"}`` release row
    (:func:`release_stand_downs`, proposal 091) provides the head-agnostic
    complement: scanned in row order, it voids any *earlier* matched verdict for
    its PR, while a verdict recorded on a *later* row still matches
    (last-write-wins). The release-aware scan lives in the adapter; this
    caller-side ``pr``/``head_sha`` guard stays here. Read-only, zero SDK cost.
    """
    if pr <= 0 or not head_sha:
        return None
    return backend.find(pr, head_sha)


def has_live_stand_down(pr: int, backend: StandDownVerdictBackend) -> bool:
    """Return whether *pr* carries an unreleased stand-down verdict, at any head.

    The head-agnostic liveness complement to :func:`find_stand_down` — used by
    the on-settlement stand-down-verdict reconciler (proposal 103) to decide
    whether appending a :func:`release_stand_downs` row would actually change the
    store, so the recurring per-cycle drive stays idempotent and never grows the
    append-only ledger without a real state change. Where :func:`find_stand_down`
    matches an exact ``(pr, head_sha)`` key, this reader ignores the head SHA
    entirely: it answers only *is any suppression for this PR currently live*,
    which is what a settlement clear (whose head SHA is immaterial) must ask.

    Delegates the release-aware, last-write-wins scan to *backend* (any verdict
    row for the PR re-arms the brake whatever its head; any release row voids it),
    keeping only the non-positive-*pr* caller-side guard here. Read-only, zero SDK
    cost.
    """
    if pr <= 0:
        return False
    return backend.has_live(pr)


def leading_same_sha_streak(laps: list[str], head: str) -> int:
    """Count the leading run of *laps* whose SHA equals *head*.

    The stalled-lineage streak the chosen-stand-down machinery measures: *laps*
    lists a PR's ``partial``-lap head SHAs most-recent-first, and the leading run
    equal to *head* is how many times the same head was re-serviced without ever
    advancing. Walks from the front, stopping at the first differing SHA. Shared
    by :func:`mechanical_standdown_verdict`, its production counter
    :func:`measure_standdown_production`, and
    :meth:`maestro.agent.MaestroAgent._mechanical_standdown_verdict` so the stall
    semantics live in one place (an empty *head* — no recorded SHA — matches any
    empty lap entry, so callers that must exclude a keyless head guard it before
    calling).
    """
    streak = 0
    for sha in laps:
        if sha != head:
            break
        streak += 1
    return streak


def mechanical_standdown_verdict(
    pr_number: int,
    head_sha: str,
    recent_partial_laps: list[str],
    has_unserviced_threads: bool,
    threshold: int,
    *,
    is_pr_merged: Callable[[int], bool] | None = None,
    now: str | None = None,
) -> StandDownVerdict | None:
    """Author a chosen-stand-down verdict mechanically, or return ``None``.

    The deterministic actuator the chosen-stand-down clause needed. It converts
    "stand down on a stalled, thread-clear PR" from an LLM choice made at the
    remediation pop — and, per the self-model, never actually made — into a
    pure predicate the loop can act on *before* opening an SDK session. Whether
    the remaining merge blocker is clearable is deliberately **not** this
    predicate's question: clearability is governed by the caller's salvage
    oracle (:func:`maestro.pr_facts.assess_pr_salvage`, consulted at the
    write site), which vetoes a give-up on salvageable work. Returns a
    :class:`StandDownVerdict` keyed to ``(pr_number, head_sha)`` — the exact
    key :func:`find_stand_down` reads — only when **all** of these hold:

    * *threshold* is positive, *pr_number* is positive, and *head_sha* is
      non-empty (a verdict with no key could never be looked up);
    * *has_unserviced_threads* is ``False`` — no review thread is waiting on an
      answer (answering threads is LLM-only work a stand-down must never skip);
    * *head_sha* is unchanged across at least *threshold* trailing ``partial``
      laps — *recent_partial_laps* lists the PR's recent ``partial``-lap head
      SHAs most-recent first, and the leading run equal to *head_sha* must reach
      *threshold* (the stalled-lineage condition: re-serviced *threshold* times
      without the head ever advancing); and
    * git-merge-truth — via *is_pr_merged* — shows the PR is **not** merged.

    The git oracle is consulted **last**, only after every cheap condition has
    passed, so a non-qualifying PR never pays for the git read. *is_pr_merged*
    defaults to :func:`maestro.audit_checks_phantom._git_pr_merged_default`
    (lazy-imported to avoid the circular import, since ``audit_checks`` imports
    this module). A new commit changes *head_sha*, so the leading-run count drops
    below *threshold* and the verdict auto-releases — the same SHA-keyed
    auto-release :func:`find_stand_down` relies on. *now* injects the ISO
    timestamp (``None`` stamps the current UTC time), mirroring
    :func:`record_stand_down`. Pure apart from the injected oracle; zero SDK
    cost. Returns ``None`` when any condition fails.
    """
    if threshold <= 0 or pr_number <= 0 or not head_sha:
        return None
    if has_unserviced_threads:
        return None
    if leading_same_sha_streak(recent_partial_laps, head_sha) < threshold:
        return None
    if is_pr_merged is None:
        from maestro.audit_checks_phantom import (
            _git_pr_merged_default,
        )

        is_pr_merged = _git_pr_merged_default
    if is_pr_merged(pr_number):
        return None
    recorded_at = now if now is not None else datetime.now(UTC).isoformat()
    return StandDownVerdict(
        pr=pr_number, head_sha=head_sha, recorded_at=recorded_at,
    )


def measure_standdown_production(
    tasks: list[dict[str, object]], backend: StandDownVerdictBackend,
) -> tuple[int, int]:
    """Count mechanical-stand-down *candidates* against verdicts actually *written*.

    The production counter for the chosen-stand-down actuator
    (:meth:`maestro.agent.MaestroAgent._dispose_mechanical_standdown`): it keeps
    the candidates-vs-written gap observable so a regression that starves the
    eviction gate — the ledger holding a lone verdict against dozens of
    ``partial`` re-laps — surfaces in contemplation instead of silently
    recurring.

    *candidates* — the count of distinct ``(pr, head_sha)`` remediation lineages
    in *tasks* (oldest-first task-history dicts, as
    :meth:`maestro.memory.Memory.get_recent_tasks` returns) whose trailing run of
    same-SHA ``partial`` laps reaches the stalled-lineage threshold
    (:data:`maestro.loop.pr_remediation.STALLED_REMEDIATION_LAP_CAP`). For each PR the laps are
    read most-recent-first, the trailing run ends at the first non-``partial``
    same-PR lap, and only the leading run whose head SHA equals the most-recent
    lap's is counted (via :func:`leading_same_sha_streak`) — the stall
    *precondition* :func:`mechanical_standdown_verdict` shares. This is
    deliberately only the streak: candidates does **not** re-verify the actuator's
    further gates (thread-clarity, git-unlanded, the caller's salvage veto), which
    would need live host/git reads. So a candidate is a lineage the actuator would
    *examine*, not necessarily one it would stand down on — candidates over-counts
    what actually gets a verdict, and the intended signal is the *trend* of the
    written-vs-candidate gap, not a per-lineage claim. The SHA is the last recorded
    lap's, not a live re-read of the branch head. A lineage carrying no PR number
    or an empty head SHA is skipped.

    *written* — *backend*'s all-time count of parseable
    ``(pr, head_sha, recorded_at)`` verdict rows (the ledger
    :func:`record_stand_down` appends and :func:`find_stand_down` reads),
    tolerating a corrupted tail exactly as :func:`find_stand_down` does. This is
    the ledger's all-time row count, **not** windowed to *tasks*, so the ratio
    pairs an all-time numerator with a current-window denominator — read it as a
    gap indicator, not an exact per-candidate hit rate.

    Both reads are zero SDK cost. Returns ``(candidates, written)``.
    """
    # Cycle-break: ``agent`` imports this module, so the stall threshold is
    # lazy-imported at call time (the same idiom :func:`mechanical_standdown_verdict`
    # uses for its git oracle default).
    from maestro.loop.pr_remediation import STALLED_REMEDIATION_LAP_CAP

    # Gather each PR's trailing run of ``partial`` laps most-recent-first. Tasks
    # arrive oldest-first, so walk reversed; the first non-``partial`` same-PR lap
    # closes that PR's run (its head advanced and real work landed), matching the
    # actuator's break condition.
    per_pr_laps: dict[int, list[str]] = {}
    closed: set[int] = set()
    for task in reversed(tasks):
        goal = task.get("goal")
        if not isinstance(goal, str):
            continue
        pr = pr_number_from_remediation_goal(goal)
        if pr is None or pr in closed:
            continue
        if task.get("outcome") != "partial":
            closed.add(pr)
            continue
        sha = task.get("pr_head_sha")
        per_pr_laps.setdefault(pr, []).append(sha if isinstance(sha, str) else "")

    candidates = 0
    for laps in per_pr_laps.values():
        # ``per_pr_laps`` values are appended into, so each is non-empty; the
        # most-recent lap's head SHA anchors the streak.
        head = laps[0]
        if not head:
            continue
        if leading_same_sha_streak(laps, head) >= STALLED_REMEDIATION_LAP_CAP:
            candidates += 1

    written = backend.written_count()
    return candidates, written


def format_standdown_production(
    tasks: list[dict[str, object]], backend: StandDownVerdictBackend,
) -> str:
    """Render a ``STAND-DOWN-PRODUCTION`` line, or ``""`` when no candidate exists.

    Calls :func:`measure_standdown_production` and, when at least one lineage sits
    at the stall threshold (a stand-down *candidate* — see that function for the
    exact, deliberately cheap measure, which does **not** re-verify the actuator's
    further conditions of blocker ownership, thread-clarity, or merge state),
    returns a single line reporting how many ``[stood-down]`` verdicts the ledger
    holds all-time against that current candidate count — the visible gap a
    starved eviction gate (one verdict against many re-laps) opens. Returns ``""``
    when there is no candidate to account for, so the caller injects the line only
    when there is one. Pure formatting apart from the two zero-SDK reads;
    measurement-only.
    """
    candidates, written = measure_standdown_production(tasks, backend)
    if candidates <= 0:
        return ""
    return (
        f"STAND-DOWN-PRODUCTION: {written}/{candidates} — chosen-stand-down "
        f"verdicts ever written to the ledger vs PR lineages now at the stall "
        f"threshold (ownership/threads/merge-state unverified here); a persistent "
        f"gap can mean the eviction gate is starved and stalled PRs keep "
        f"re-lapping through full remediation sessions"
    )


# ---------------------------------------------------------------------------
# Base-advance race convergence sensor (proposal 099) — zero-SDK, read-only.
# A moved ``origin/main`` under a PR is a fresh conflict-resolution race, and the
# re-entry behaviour re-keys its give-up conditions on that movement. This sensor
# makes the convergence of that re-entry observable — races re-run vs races won —
# so the operator can tell "re-entry converts races to merges" from "re-entry
# spins the same PR every lap without landing". Measurement only; it reads no git
# or host, changes no stop condition, and is independent of the re-entry
# behaviour. Self-suppresses until real base-advance data exists.
# ---------------------------------------------------------------------------

# Minimum remediation lineages before :func:`format_base_advance_races` surfaces
# its line — below this the base-advance data is too thin to read as a
# convergence signal, so the line self-suppresses (the minimum-sample idiom of
# :data:`_REFUSAL_PROFILE_DATA_FLOOR`). Base-advance data only accrues once the
# re-entry behaviour has run for a while, so this floor keeps the readout dark
# until there is a window worth watching.
_BASE_ADVANCE_RACES_DATA_FLOOR = 3


def _read_merged_settled_prs(backend: SettledVerdictsBackend) -> set[int]:
    """Return the PRs whose collapsed settled-verdicts row is a merged settle.

    A thin reader over :meth:`~maestro.audit_checks_settlement.SettledVerdictsBackend.merged_settled_prs`
    — the ``kind == "merged"`` last-row-wins collapse now owned by the backend
    (:class:`~maestro.audit_checks_settlement.JsonlSettledVerdictsLedger` by default, or the
    shared SQLite store behind ``[paths] sqlite_state_enabled``). The row-shape
    discrimination, corruption tolerance, and single-home ownership of the ledger
    filename all live with the backend, so this reader no longer walks the file
    directly nor needs the former lazy ``audit_checks`` import-cycle dodge. Zero
    SDK cost.
    """
    return set(backend.merged_settled_prs())


def _group_remediation_laps_by_pr(
    tasks: list[dict[str, object]],
) -> dict[int, list[str | None]]:
    """Group remediation laps in *tasks* by PR, yielding each lap's base watermark.

    Walks *tasks* (oldest-first task-history dicts) and, for each lap whose
    ``goal`` names a remediation PR (via :func:`pr_number_from_remediation_goal`),
    appends that lap's ``base_head_sha`` — the ``origin/main`` base it raced
    against — to the PR's list, preserving task order. A lap missing its base (a
    legacy record, a non-string, or an empty string) contributes ``None``, so it
    carries no base watermark for the re-entry walk. Shared by
    :func:`measure_base_advance_races` and
    :func:`measure_unwinnable_race_re_entries`; zero SDK cost, no git or host read.
    """
    per_pr_laps: dict[int, list[str | None]] = {}
    for task in tasks:
        goal = task.get("goal")
        if not isinstance(goal, str):
            continue
        pr = pr_number_from_remediation_goal(goal)
        if pr is None:
            continue
        base = task.get("base_head_sha")
        per_pr_laps.setdefault(pr, []).append(
            base if isinstance(base, str) and base else None
        )
    return per_pr_laps


def _count_base_re_entries(bases: list[str | None]) -> int:
    """Count how many times a lineage re-entered the race on a moved base.

    Walks a lineage's per-lap ``base_head_sha`` watermarks in task order and
    counts each transition where a lap's base differs from the last lap that
    carried one — a moved base is a fresh race, so it re-entered. A ``None``
    watermark (a legacy/non-string/empty base) neither counts as a re-entry nor
    updates the remembered base: the next lap that has a base compares against
    the last one that did. Returns ``0`` for a lineage that never moved its base.
    """
    prior: str | None = None
    count = 0
    for base in bases:
        if base is None:
            continue
        if prior is not None and base != prior:
            count += 1
        prior = base
    return count


def measure_base_advance_races(
    tasks: list[dict[str, object]], backend: SettledVerdictsBackend,
) -> dict[str, object]:
    """Measure conflict-remediation base-advance races run versus won.

    A remediation lap records both its lap-start PR head watermark (``pr_head_sha``) and
    the ``origin/main`` base it raced against (``base_head_sha``). When the base
    moves under a PR between laps, the re-entry behaviour re-keys its give-up
    conditions and re-enters the resolution race — the case whose convergence
    this sensor makes observable, so the operator can tell "re-entry converts
    races to merges" from "re-entry spins the same PR every lap without landing".

    Groups the remediation laps in *tasks* (oldest-first task-history dicts, as
    :meth:`maestro.memory.Memory.get_recent_tasks` returns) by PR via
    :func:`pr_number_from_remediation_goal`. For each lineage, walks its laps in
    task order and marks it *re-entered* when a lap's ``base_head_sha`` differs
    from the lineage's prior-lap base — a moved base is a fresh race. A lap
    missing its ``base_head_sha`` (a legacy record, or a non-string value) carries
    no base watermark, so it neither marks a re-entry nor updates the remembered
    base; the next lap that has a base compares against the last one that did. A
    lineage is *won* when it re-entered **and** its PR is a merged settle in the
    settled-verdicts pre-filter *backend* (via :func:`_read_merged_settled_prs` →
    :meth:`~maestro.audit_checks_settlement.SettledVerdictsBackend.merged_settled_prs`,
    ``kind == "merged"`` last-row-wins per ``pr_number``).

    Returns::

        {
            "lineages": int,         # distinct remediation PR lineages seen
            "re_entered": int,       # lineages that re-entered on >=1 base advance
            "won": int,              # re-entered lineages whose PR merged
            "race_won_ratio": float, # won / re_entered (0.0 when re_entered == 0)
        }

    ``won`` is a subset of ``re_entered`` (a merge that never re-raced a moved
    base is not a race won), so ``race_won_ratio`` reads as "of the PRs that
    re-raced a moved base, what share converted to a merge." Both reads (the
    in-memory task list, the backend) are zero SDK cost.
    """
    per_pr_laps = _group_remediation_laps_by_pr(tasks)
    merged_prs = _read_merged_settled_prs(backend)
    lineages = 0
    re_entered = 0
    won = 0
    for pr, bases in per_pr_laps.items():
        lineages += 1
        if _count_base_re_entries(bases) > 0:
            re_entered += 1
            if pr in merged_prs:
                won += 1
    return {
        "lineages": lineages,
        "re_entered": re_entered,
        "won": won,
        "race_won_ratio": won / re_entered if re_entered > 0 else 0.0,
    }


def format_base_advance_races(
    tasks: list[dict[str, object]], backend: SettledVerdictsBackend,
) -> str:
    """Render a ``BASE-ADVANCE-RACES`` line, or ``""`` below the data floor.

    Calls :func:`measure_base_advance_races` (over the settled-verdicts pre-filter
    *backend*) and returns ``""`` when fewer than
    :data:`_BASE_ADVANCE_RACES_DATA_FLOOR` remediation lineages are present — the
    minimum-sample self-suppression of :func:`format_refusal_profile`, so the line
    stays dark until there is a window of base-advance data worth watching (it
    only accrues once the re-entry behaviour has run). Otherwise returns a single
    line reporting how many re-entered races converted to a merge against how many
    re-entered at all — the falsifiable convergence readout that separates
    re-entry-is-working (races land) from re-entry-is-spinning (races recur
    without landing). Pure apart from the
    two zero-SDK reads; measurement-only (changes no behaviour).
    """
    races = measure_base_advance_races(tasks, backend)
    lineages: int = races["lineages"]  # type: ignore[assignment]
    if lineages < _BASE_ADVANCE_RACES_DATA_FLOOR:
        return ""
    re_entered: int = races["re_entered"]  # type: ignore[assignment]
    won: int = races["won"]  # type: ignore[assignment]
    ratio: float = races["race_won_ratio"]  # type: ignore[assignment]
    return (
        f"BASE-ADVANCE-RACES: {won}/{re_entered} races won ({ratio:.0%}) — "
        f"conflict-remediation lineages that re-entered the resolution race on a "
        f"moved origin/main and then merged, over {lineages} remediation "
        f"lineages; a ratio near zero with re-entries climbing means re-entry is "
        f"spinning the same PR without landing, not converting races to merges"
    )


def measure_unwinnable_race_re_entries(
    tasks: list[dict[str, object]],
) -> dict[int, int]:
    """Count per-PR base-advance re-entries for the unwinnable-race escalation.

    Proposal 099 (step 8). Groups the remediation laps in *tasks* (oldest-first
    task-history dicts, as :meth:`maestro.memory.Memory.get_recent_tasks`
    returns) by PR via :func:`pr_number_from_remediation_goal` and returns, per
    PR lineage, how many times it re-entered the resolution race on a moved
    ``origin/main`` base — the same per-lap base-movement walk
    :func:`measure_base_advance_races` performs, but returning the re-entry
    *count* per PR rather than an aggregate won/re-entered ratio. A lineage whose
    base never moved (or that carries no measurable base) reports ``0``.

    The escalation actuator
    (:func:`maestro.loop.self_audit.audit_self_model`) fires the message-only,
    no-standdown heads-up for a PR whose count reaches ``[loop]
    unwinnable_race_re_entry_threshold`` and that has not landed. Unlike
    :func:`measure_base_advance_races` this reads no ledger — the merge-truth
    filter lives at the actuator (via ``is_pr_settled``), so this is a pure walk
    over the in-memory task list with zero SDK cost and no git or host read.
    """
    return {
        pr: _count_base_re_entries(bases)
        for pr, bases in _group_remediation_laps_by_pr(tasks).items()
    }


# ---------------------------------------------------------------------------
# Confirmed-vs-caused landing attribution — distinguish a cure I *moved* from a
# cure I merely *confirmed* was already landed before I touched it.
# ---------------------------------------------------------------------------
#
# Every sibling sensor above asks whether a remediation landed at all. This one
# asks the subtler question the Release-not-resolve contract hinges on: of the
# cures I stamp ``success``, how many did I CAUSE — a merge/commit/conflict
# resolution performed during or because of my task — versus merely CONFIRM were
# already merged before I arrived? Counting another's land as my own is the next
# costume of the disease ('Confirmed-isn't-caused'): six of ten recent tasks
# ended at an already-merged PR, and one merge was counted as mine four times.
#
# The classifier reads only the recorded ``result_summary`` (zero SDK cost),
# mirroring :func:`maestro.audit_checks.classify_settled_relap`. CONFIRMED is
# checked *first*: a summary stating the PR was already landed is a confirmation
# even when it also uses merge/commit words — the sensor must never over-claim
# CAUSED, since over-claiming is precisely the blind spot it exists to expose.
# Measurement-only: it surfaces the ratio, it changes no behavior.

# Outcomes whose ``result_summary`` carries a landing claim worth attributing.
# Mirrors :data:`maestro.audit_checks._SUCCESS_OUTCOMES`.
_LANDING_SUCCESS_OUTCOMES: frozenset[str] = frozenset({"success"})

# Markers that the cure was ALREADY landed before this task touched it — a
# confirmation of another's merge, not a landing I caused. Matched
# case-insensitively (all literals lowercase).
_CONFIRMED_LANDING_MARKERS: tuple[str, ...] = (
    # Keyword-bearing "already merged / landed / …" prose lives in
    # ``narration_cues`` so the record-time scrub protects it (#2970); the
    # keyword-free confirmation markers stay local.
    *_CONFIRMED_LANDING_PHRASES,
    "already on main",
    "already closed",
    "before this task",
    "before i touched",
    "premise false",
    "false premise",
    "premise was false",
    "no longer open",
)

# Markers that THIS task performed the work that moved the cure — a merge of
# origin/main, an on-branch commit, a conflict resolution, a failing test plus
# fix. Matched case-insensitively (all literals lowercase).
_CAUSED_LANDING_MARKERS: tuple[str, ...] = (
    # Keyword-bearing "merged … / resolved … / fixed …" prose lives in
    # ``narration_cues`` so the record-time scrub protects it (#2970); the
    # keyword-free work markers stay local.
    *_CAUSED_LANDING_PHRASES,
    "conflict resolution",
    "committed the fix",
    "committed a fix",
    "committed the resolution",
    "committed on-branch",
    "committed on branch",
    "committed to the branch",
    "wrote the failing test",
    "wrote a failing test",
    "added the failing test",
)

# Attribution labels returned by :func:`classify_landing_attribution`.
LandingAttribution = Literal["CAUSED", "CONFIRMED", "NA"]


def classify_landing_attribution(task: dict[str, object]) -> LandingAttribution:
    """Attribute a ``success`` task's landing as CAUSED, CONFIRMED, or NA.

    Reads only *task*'s ``outcome`` and ``result_summary`` (zero SDK cost),
    mirroring :func:`maestro.audit_checks.classify_settled_relap`. Returns:

    - ``"NA"`` when *task* is not a success outcome, has no string
      ``result_summary``, or its summary carries neither a caused- nor a
      confirmed-marker (the landing cannot be attributed from the record).
    - ``"CONFIRMED"`` when the summary matches any
      :data:`_CONFIRMED_LANDING_MARKERS` — the cure was already landed before
      this task touched it (a confirmation of another's merge).
    - ``"CAUSED"`` when the summary matches any :data:`_CAUSED_LANDING_MARKERS`
      — this task performed the merge/commit/conflict-resolution that moved it.

    CONFIRMED is checked **first**: a summary stating the PR was already merged
    is a confirmation even when it also uses merge/commit words, so the sensor
    never over-claims CAUSED — over-claiming another's land as my own is exactly
    the blind spot ('Confirmed-isn't-caused') this instrument exists to expose.

    Pure function — zero SDK cost.
    """
    outcome = task.get("outcome")
    if not isinstance(outcome, str) or outcome not in _LANDING_SUCCESS_OUTCOMES:
        return "NA"
    summary = task.get("result_summary")
    if not isinstance(summary, str) or not summary.strip():
        return "NA"
    lowered = summary.lower()
    if any(marker in lowered for marker in _CONFIRMED_LANDING_MARKERS):
        return "CONFIRMED"
    if any(marker in lowered for marker in _CAUSED_LANDING_MARKERS):
        return "CAUSED"
    return "NA"


def measure_confirmed_landing_rate(
    data_dir: Path, n: int = 0,
) -> dict[str, object]:
    """Measure the confirmed-vs-caused landing ratio among recent successes.

    Reads the trailing *n* records of ``task_history.jsonl`` under *data_dir*
    via :func:`_read_recent_task_tail` (``n`` ≤ 0 reads effectively all,
    mirroring :func:`maestro.audit_checks.measure_relap_rate`), classifies each
    via :func:`classify_landing_attribution`, and returns::

        {
            "caused": int,            # landings this task moved
            "confirmed": int,         # landings already done before the task
            "total": int,             # caused + confirmed (attributable)
            "confirmed_rate": float,  # confirmed / total (0.0 if total == 0)
        }

    ``confirmed_rate`` is the share of attributable landings that were merely
    confirmations of an already-landed cure rather than ones I caused — the
    line the Release-not-resolve contract hinges on. The denominator excludes
    ``NA`` records, so both the empty-history and all-``NA`` cases yield a total
    of ``0`` and a rate of ``0.0`` (no attributable landing to report).

    Reads from disk; otherwise pure — zero SDK cost.
    """
    tasks = _read_recent_task_tail(data_dir, n if n and n > 0 else 10_000)
    caused = 0
    confirmed = 0
    for task in tasks:
        cls = classify_landing_attribution(task)
        if cls == "CAUSED":
            caused += 1
        elif cls == "CONFIRMED":
            confirmed += 1
    total = caused + confirmed
    return {
        "caused": caused,
        "confirmed": confirmed,
        "total": total,
        "confirmed_rate": confirmed / total if total > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Verdict-needs-fetch gate — the enforcement floor under fetch-before-word
# ---------------------------------------------------------------------------
#
# Every sibling above this line *measures* the wheel; the five pending plans
# circling this same failure all *advise* — a prompt preamble or an
# assessment-ordering nudge. None remove the freedom to write the verdict
# before the hands move. This gate is the floor beneath those ceilings: a
# remediation status-write that claims the cure resolved on-branch ("awaiting
# tend push" / CONFLICTING / AWAITING_PUSH) is physically blocked unless a
# per-task fetch-truth marker proves ``git fetch`` + ``--is-ancestor`` ran
# first. The sequence — mouth before hands — was the whole gap: nine of ten
# recent remediations stamped the phrase as if it were an outcome; the one
# exception fetched first and landed a success because the conflict was
# phantom. :func:`record_fetch_truth` writes the marker at the fetch
# action-zero; :func:`gate_remediation_verdict` downgrades any verdict the
# marker does not back. Deliberately mints NO sensor — this gate blocks and
# downgrades, it does not observe.

# Per-task fetch-evidence marker ledger written by :func:`record_fetch_truth`.
_FETCH_TRUTH_FILE = "fetch_truth_markers.jsonl"

# Forced outcome marker stamped onto a verdict whose fetch-truth is missing.
_FETCH_MISSING_MARKER = "[fetch-missing]"

# Verdict narratives that may only be stamped after git has spoken: the
# on-branch re-lap costume (:attr:`Marker.AWAITING_TEND_PUSH`) and the two
# Tend-clearable merge-block reason codes. A success/partial carrying any of
# these claims the resolution lives on-branch awaiting a push — exactly the
# claim that must be backed by a real ``--is-ancestor`` read, never asserted.
# Matched case-insensitively against the verdict text (literals lowercased).
_FETCH_REQUIRED_NARRATIVES: tuple[str, ...] = (
    Marker.AWAITING_TEND_PUSH,
    # A delivered-marker verdict still requires a recorded fetch-truth (the lap
    # must have called ``_record_remediation_fetch_truth``) so
    # ``gate_remediation_verdict`` applies equally to both push markers
    # (proposal 091 step 5).
    Marker.DELIVERED_AWAITING_MERGE,
    # An owned-push verdict (proposal 098) is a claim about where the commit
    # is, so it owes the same fetch-truth proof as the two push markers it now
    # replaces on the non-delivery arms: without a recorded
    # ``record_fetch_truth`` call the verdict spoke before git did.
    Marker.OWNED_PUSH_AVAILABLE,
    # A push-unconfirmed verdict (proposal 099) makes the same on-branch,
    # un-pushed claim as OWNED_PUSH_AVAILABLE, so it owes the same fetch-truth
    # proof. Before the marker split a no-op push stamped
    # DELIVERED_AWAITING_MERGE (also required here); listing PUSH_UNCONFIRMED
    # keeps the fetch-before-word floor under exactly those verdicts.
    Marker.PUSH_UNCONFIRMED,
    MERGE_BLOCK_CONFLICTING.lower(),
    MERGE_BLOCK_AWAITING_PUSH.lower(),
)

# ---------------------------------------------------------------------------
# Obligation-fork marker family — owed-push separated from awaited-merge
# ---------------------------------------------------------------------------
#
# The fetch-truth marker above proves *git spoke* — a ``git merge-base
# --is-ancestor HEAD origin/main`` read ran before any status word. That is the
# *blameless* read: it answers "did this land?" (no, no owner). It never forks
# the two distinct facts an ``awaiting`` verdict fuses on an owned unpushed head:
# **I owe a push** (local HEAD is ahead of ``origin/<branch>``) vs **Thomas owes
# a merge** (HEAD is already on ``origin/<branch>``, the push discharged). Seven
# consecutive AWAITING_PUSH remediations filed ``partial: awaiting review/merge``
# without ever running the *indicting* read — ``git rev-parse HEAD`` vs ``git
# rev-parse origin/<branch>`` — that separates them.
#
# This marker family records that fork. :func:`record_obligation_fork_truth`
# persists the observed HEAD and ``origin/<branch>`` SHAs per PR/turn;
# :func:`gate_remediation_verdict` reads them so an owned AWAITING_PUSH verdict
# can no longer disguise an un-advanced owned head as external deferral. Like the
# fetch-truth gate above, this floor blocks and downgrades — it mints no sensor.

# Per-task obligation-fork marker ledger written by
# :func:`record_obligation_fork_truth`. A sibling of :data:`_FETCH_TRUTH_FILE`.
_OBLIGATION_FORK_FILE = "obligation_fork_markers.jsonl"

# Forced outcome marker stamped onto an owned AWAITING_PUSH verdict that ran no
# obligation-fork read at all — the fork compare was never performed, so the
# verdict cannot know whether the push is owed.
_FORK_MISSING_MARKER = "[fork-missing]"

# Forced outcome marker stamped when the obligation-fork read proves local HEAD
# is ahead of ``origin/<branch>`` yet the verdict wears external-deferral
# phrasing: the push is the agent's own owed act, not an external wait.
_PUSH_OWED_MARKER = "[push-owed]"

# External-deferral phrasings an owned AWAITING_PUSH verdict fuses "I owe a push"
# and "Thomas owes a merge" into. When one appears on an owned AWAITING_PUSH
# verdict, an obligation-fork marker proving the push is discharged (HEAD ==
# ``origin/<branch>``) is required before the deferral may stand. Matched
# case-insensitively against the verdict text (every literal is lowercase).
# ``awaiting review/merge`` is the tail of :attr:`Marker.DELIVERED_AWAITING_MERGE`
# and ``awaiting tend push`` is :attr:`Marker.AWAITING_TEND_PUSH`; both name the
# handoff-to-someone-else the fork read must justify.
_EXTERNAL_DEFERRAL_NARRATIVES: tuple[str, ...] = (
    Marker.AWAITING_TEND_PUSH,
    "awaiting review/merge",
    "awaiting review",
    "awaiting merge",
)


@dataclass(frozen=True)
class ObligationFork:
    """The HEAD-vs-``origin/<branch>`` fork a remediation lap observed.

    *head_sha* is the local ``HEAD`` SHA (what a push would deliver); *remote_sha*
    is the ``origin/<branch>`` SHA the fetch observed; *branch* is the PR branch
    name. *remote_is_ancestor* records whether ``origin/<branch>`` is an ancestor
    of local ``HEAD`` — i.e. HEAD is a fast-forward descendant, genuinely *ahead*
    rather than merely diverged or behind. :attr:`head_ahead` is the indicting
    fork: ``True`` only when HEAD is genuinely ahead (a plain ``git push`` would
    fast-forward), ``False`` when the two match (the push is discharged and the
    remaining wait is genuinely external) *or* when the branch has diverged/fallen
    behind (a reconcile is owed, not a fast-forward push).
    """

    head_sha: str
    remote_sha: str
    branch: str
    remote_is_ancestor: bool = False

    @property
    def head_ahead(self) -> bool:
        """Return True only when local HEAD is genuinely ahead of origin/<branch>.

        Two conditions must both hold: the SHAs differ *and* ``origin/<branch>``
        is an ancestor of ``HEAD`` (*remote_is_ancestor*), so a plain ``git push``
        would fast-forward. A bare SHA inequality is not enough — inequality is
        also true when HEAD is *behind or diverged from* origin/<branch> (e.g. a
        stale local PR branch whose origin counterpart Tend advanced), where a
        reconcile/merge is owed, not a push. Coercing "the push is mine to fire"
        there would be factually inverted, so the ancestry proof gates it.

        Because an empty/unreadable ``origin/<branch>`` cannot be proven an
        ancestor, *remote_is_ancestor* is ``False`` for it and the fork reports no
        push owed — the "do not coerce without proof" safety holds for any
        unreadable read, symmetric or not.
        """
        return self.remote_is_ancestor and self.head_sha != self.remote_sha


def record_obligation_fork_truth(
    task_id: str,
    pr_number: int,
    head_sha: str,
    remote_sha: str,
    branch: str,
    data_dir: Path,
    *,
    remote_is_ancestor: bool = False,
) -> None:
    """Append an obligation-fork marker recording HEAD vs origin/<branch>.

    Records that *task_id* (remediating *pr_number*) ran the ownership-fork
    action-zero — ``git rev-parse HEAD`` (*head_sha*) vs ``git rev-parse
    origin/<branch>`` (*remote_sha*) — so :func:`gate_remediation_verdict` can
    later fork "I owe a push" (HEAD genuinely ahead) from "Thomas owes a merge"
    (HEAD == origin/<branch>) rather than let an ``awaiting`` verdict fuse them.
    *remote_is_ancestor* carries whether ``origin/<branch>`` is an ancestor of
    ``HEAD`` (``git merge-base --is-ancestor origin/<branch> HEAD``), so the gate
    distinguishes a genuinely fast-forwardable *ahead* head from a diverged/behind
    one where a reconcile — not a push — is owed. One JSONL line per PR/turn is
    appended to ``obligation_fork_markers.jsonl`` under *data_dir*, guarded by
    :func:`~maestro.fileutil.file_lock`.

    **Idempotent per PR/turn**: a marker already recorded for this
    ``(task_id, pr_number)`` pair is not re-appended, so a second call in the
    same turn does not duplicate the row. An empty *task_id* carries no key to
    match on, so it is ignored rather than written. Append-only; consumed
    read-only by :func:`get_obligation_fork`.
    """
    if not task_id:
        return
    path = data_dir / _OBLIGATION_FORK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        for row in _read_obligation_fork_rows(path):
            if (
                row.get("task_id") == task_id
                and row.get("pr_number") == pr_number
            ):
                # Already recorded this PR/turn — idempotent no-op.
                return
        record = json.dumps(
            {
                "task_id": task_id,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "remote_sha": remote_sha,
                "branch": branch,
                "remote_is_ancestor": remote_is_ancestor,
            },
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(record + "\n")


def _read_obligation_fork_rows(path: Path) -> list[dict[str, object]]:
    """Return the parsed obligation-fork marker rows at *path*, tolerant of gaps.

    Returns an empty list when the file is absent or unreadable; malformed lines
    are skipped (corrupted-tail tolerance). Append-order is preserved. Read-only.
    """
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return []
    rows: list[dict[str, object]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def get_obligation_fork(task_id: str, data_dir: Path) -> ObligationFork | None:
    """Return the obligation-fork marker for *task_id*, or ``None`` when absent.

    Scans ``obligation_fork_markers.jsonl`` under *data_dir* for the marker whose
    ``task_id`` matches, returning the last matching row as an
    :class:`ObligationFork`. Returns ``None`` when *task_id* is empty, the file
    is absent or unreadable, or no well-formed row matches — the gate then treats
    the owned AWAITING_PUSH verdict as fork-unread and downgrades it. Malformed
    lines and rows missing any of the three string fields are skipped. A legacy
    row lacking ``remote_is_ancestor`` defaults it to ``False`` (no ancestry
    proof ⇒ not genuinely ahead), the "do not coerce without proof" safe
    direction. Read-only, zero SDK cost.
    """
    if not task_id:
        return None
    found: ObligationFork | None = None
    for row in _read_obligation_fork_rows(data_dir / _OBLIGATION_FORK_FILE):
        if row.get("task_id") != task_id:
            continue
        head = row.get("head_sha")
        remote = row.get("remote_sha")
        branch = row.get("branch")
        if (
            isinstance(head, str)
            and isinstance(remote, str)
            and isinstance(branch, str)
        ):
            found = ObligationFork(
                head_sha=head,
                remote_sha=remote,
                branch=branch,
                remote_is_ancestor=bool(row.get("remote_is_ancestor", False)),
            )
    return found


@dataclass(frozen=True)
class ObligationForkRecord:
    """The full on-disk obligation-fork marker row (proposal 106).

    Carries every column ``record_obligation_fork_truth`` writes — including the
    ``task_id`` and ``pr_number`` keys the four-field reader view
    :class:`ObligationFork` drops — so the SQLite store can round-trip the whole
    row and reproduce the JSONL writer's per-``(task_id, pr_number)`` idempotency.
    The seed source :func:`load_all_obligation_forks` returns these; the store
    reader :meth:`~maestro.state_store.StateStore.get_obligation_fork_for_task`
    hands back the narrower :class:`ObligationFork` the gate consumes.
    """

    task_id: str
    pr_number: int
    head_sha: str
    remote_sha: str
    branch: str
    remote_is_ancestor: bool = False


def load_all_obligation_forks(data_dir: Path) -> list[ObligationForkRecord]:
    """Return every obligation-fork marker oldest-first — the SQLite seed source.

    Reads the whole ``obligation_fork_markers.jsonl`` in file (append / oldest
    -first) order via :func:`_read_obligation_fork_rows`, sharing that reader's
    corrupted-tail tolerance. Applies the same field coercion
    :func:`get_obligation_fork` uses — a row missing any of the three string
    fields (``head_sha`` / ``remote_sha`` / ``branch``) is skipped, and a legacy
    row lacking ``remote_is_ancestor`` defaults it to ``False`` (no ancestry
    proof ⇒ not genuinely ahead). A row lacking the ``task_id`` / ``pr_number``
    keys the store needs (the writer always emits both, so their absence marks a
    hand-edited or corrupt row) is likewise skipped. Because
    :func:`get_obligation_fork` never inspects ``pr_number``, a corrupt legacy
    row whose ``pr_number`` is missing or non-``int`` (but whose three string
    fields are valid) is still readable pre-flip yet dropped by this seed
    post-flip — an accepted seed asymmetry, reachable only by a hand-edited or
    corrupt file since the writer always emits an ``int`` ``pr_number``. Seeding
    the SQLite store in
    this chronological order lets its ``AUTOINCREMENT`` ``id`` reproduce the
    append order, so a later highest-``id`` read matches the JSONL last-match
    walk. It has a real call site (the
    :class:`~maestro.state_store.SqliteObligationForkLedger` one-time seed), so
    it does not trip the self-review uncalled-symbol gate.
    """
    records: list[ObligationForkRecord] = []
    for row in _read_obligation_fork_rows(data_dir / _OBLIGATION_FORK_FILE):
        task_id = row.get("task_id")
        pr_number = row.get("pr_number")
        head = row.get("head_sha")
        remote = row.get("remote_sha")
        branch = row.get("branch")
        if (
            isinstance(task_id, str)
            and task_id
            and isinstance(pr_number, int)
            and not isinstance(pr_number, bool)
            and isinstance(head, str)
            and isinstance(remote, str)
            and isinstance(branch, str)
        ):
            records.append(
                ObligationForkRecord(
                    task_id=task_id,
                    pr_number=pr_number,
                    head_sha=head,
                    remote_sha=remote,
                    branch=branch,
                    remote_is_ancestor=bool(
                        row.get("remote_is_ancestor", False),
                    ),
                ),
            )
    return records


@runtime_checkable
class ObligationForkBackend(Protocol):
    """The obligation-fork marker persistence surface (proposal 106).

    One writer (the ownership-fork action-zero in ``MaestroAgent``) and one
    reader (:func:`gate_remediation_verdict`'s owned-``AWAITING_PUSH`` floor) go
    through this seam, so the marker can live in ``obligation_fork_markers.jsonl``
    or in ``state.db`` behind ``[paths] sqlite_state_enabled`` with no call-site
    branching. Mirrors the :class:`PrProvenanceBackend` idiom;
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    agent's backend-selection tests.
    """

    def record(
        self,
        task_id: str,
        pr_number: int,
        head_sha: str,
        remote_sha: str,
        branch: str,
        *,
        remote_is_ancestor: bool = False,
    ) -> None:
        """Append one marker (idempotent per ``(task_id, pr_number)``)."""
        ...

    def get(self, task_id: str) -> ObligationFork | None:
        """Return the last marker for *task_id*, or ``None`` when absent."""
        ...


class JsonlObligationForkLedger:
    """JSONL-backed :class:`ObligationForkBackend` — the default, flag-off store.

    Binds a ``data_dir`` and delegates ``record`` / ``get`` to the existing
    module-level :func:`record_obligation_fork_truth` / :func:`get_obligation_fork`
    — which stay the JSONL write / read core, so the writer's
    per-``(task_id, pr_number)`` idempotency guard and the shared
    ``tests/test_remediation_tracker.py`` coverage keep one code path.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def record(
        self,
        task_id: str,
        pr_number: int,
        head_sha: str,
        remote_sha: str,
        branch: str,
        *,
        remote_is_ancestor: bool = False,
    ) -> None:
        record_obligation_fork_truth(
            task_id,
            pr_number,
            head_sha,
            remote_sha,
            branch,
            self._data_dir,
            remote_is_ancestor=remote_is_ancestor,
        )

    def get(self, task_id: str) -> ObligationFork | None:
        return get_obligation_fork(task_id, self._data_dir)


def record_fetch_truth(
    task_id: str,
    pr_number: int,
    is_ancestor: bool,
    data_dir: Path,
    head_sha: str = "",
) -> None:
    """Append a per-task fetch-truth marker proving git spoke before the verdict.

    Records that *task_id* (remediating *pr_number*) ran the fetch-before-word
    action-zero — ``git fetch`` followed by a ``git merge-base --is-ancestor``
    read whose verdict is *is_ancestor* — so :func:`gate_remediation_verdict`
    can later prove a status word did not precede git's verdict. One JSONL line
    per call is appended to ``fetch_truth_markers.jsonl`` under *data_dir*,
    guarded by :func:`~maestro.fileutil.file_lock`. Append-only; consumed
    read-only by :func:`has_fetch_truth` and :func:`fetch_truth_relap`.

    *head_sha* is the origin head SHA the ancestry read observed. It is written
    only when non-empty, so a row whose head could not be resolved keeps the
    legacy three-field shape (readers tolerate the missing field); when present
    it lets :func:`fetch_truth_relap` prove the *same* head was already read
    unlanded on an earlier lap.

    An empty *task_id* carries no key to match on, so it is ignored rather than
    written — a marker that could never be looked up must never be persisted.
    """
    if not task_id:
        return
    path = data_dir / _FETCH_TRUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    marker: dict[str, object] = {
        "task_id": task_id,
        "pr_number": pr_number,
        "is_ancestor": bool(is_ancestor),
    }
    if head_sha:
        marker["head_sha"] = head_sha
    record = json.dumps(marker)
    with file_lock(path), open(path, "a", encoding="utf-8") as f:
        f.write(record + "\n")


def _read_fetch_truth_rows(path: Path) -> list[dict[str, object]]:
    """Return the parsed fetch-truth marker rows at *path*, tolerant of gaps.

    Returns an empty list when the file is absent or unreadable; malformed lines
    are skipped (corrupted-tail tolerance). Append-order is preserved. Read-only.
    Shared by :func:`has_fetch_truth`, :func:`fetch_truth_relap`, and
    :func:`load_all_fetch_truth_markers` so every fetch-truth read goes through
    one parser, mirroring :func:`_read_obligation_fork_rows`.
    """
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return []
    rows: list[dict[str, object]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def has_fetch_truth(task_id: str, data_dir: Path) -> bool:
    """Return True when a fetch-truth marker exists for *task_id*.

    Scans ``fetch_truth_markers.jsonl`` under *data_dir* for any record whose
    ``task_id`` matches. Returns ``False`` when *task_id* is empty, the file is
    absent or unreadable, or no line matches — the gate then treats the verdict
    as fetch-unbacked and downgrades it. Malformed lines are skipped
    (corrupted-tail tolerance, via :func:`_read_fetch_truth_rows`). Read-only,
    zero SDK cost.
    """
    if not task_id:
        return False
    return any(
        row.get("task_id") == task_id
        for row in _read_fetch_truth_rows(data_dir / _FETCH_TRUTH_FILE)
    )


def fetch_truth_relap(pr_number: int, head_sha: str, data_dir: Path) -> bool:
    """Return True when the ledger already proved *(pr_number, head_sha)* unlanded.

    Scans ``fetch_truth_markers.jsonl`` under *data_dir* for any marker whose
    ``pr_number`` and ``head_sha`` both match *and* whose ``is_ancestor`` is
    ``False`` — i.e. a prior lap ran the fetch-before-word action-zero against
    this exact head SHA and git said it is not yet on ``main``. A second pop for
    the same head is then a confirmed no-op: nothing changed, so re-running the
    ritual would re-prove the identical result. The owned-relap divert reads this
    to short-circuit to reconcile-or-close after a single lap.

    Returns ``False`` when *head_sha* is empty (an unresolvable head must never
    divert), the file is absent or unreadable, or no matching ``is_ancestor``
    -false marker exists — a changed head SHA, an ``is_ancestor``-true marker,
    and a legacy row lacking the ``head_sha`` field all fall through to the
    ordinary lap-count routing. Malformed lines are skipped (corrupted-tail
    tolerance, mirroring :func:`has_fetch_truth`). Read-only, zero SDK cost.
    """
    if not head_sha:
        return False
    for row in _read_fetch_truth_rows(data_dir / _FETCH_TRUTH_FILE):
        if (
            row.get("pr_number") == pr_number
            and row.get("head_sha") == head_sha
            and row.get("is_ancestor") is False
        ):
            return True
    return False


@dataclass(frozen=True)
class FetchTruthRecord:
    """The full on-disk fetch-truth marker row (proposal 106).

    Carries every column :func:`record_fetch_truth` writes — ``task_id``,
    ``pr_number``, the ``is_ancestor`` verdict, and the ``head_sha`` the ancestry
    read observed (``""`` for a legacy row whose head could not be resolved, which
    the writer emits in the three-field shape) — so the SQLite store can
    round-trip the whole row. The seed source :func:`load_all_fetch_truth_markers`
    returns these. Mirrors :class:`ObligationForkRecord`.
    """

    task_id: str
    pr_number: int
    is_ancestor: bool
    head_sha: str = ""


def load_all_fetch_truth_markers(data_dir: Path) -> list[FetchTruthRecord]:
    """Return every fetch-truth marker oldest-first — the SQLite seed source.

    Reads the whole ``fetch_truth_markers.jsonl`` in file (append / oldest-first)
    order via :func:`_read_fetch_truth_rows`, sharing that reader's corrupted-tail
    tolerance. A row missing any of the three fields the store needs — ``task_id``
    (non-empty ``str``), ``pr_number`` (``int``, never ``bool``), ``is_ancestor``
    (``bool``) — is skipped as hand-edited or corrupt, since
    :func:`record_fetch_truth` always emits all three. The optional ``head_sha``
    defaults to ``""`` when absent (the legacy three-field shape the writer emits
    when the head could not be resolved) and a non-``str`` value skips the row.
    Because :func:`record_fetch_truth` appends one row per call with no per-key
    dedupe, every marker is preserved — unlike the idempotent obligation-fork
    seed. Seeding in this chronological order lets the store's ``AUTOINCREMENT``
    ``id`` reproduce the append order, so a later read matches the JSONL walk. It
    has a real call site (the
    :class:`~maestro.state_store.SqliteFetchTruthLedger` one-time seed), so it
    does not trip the self-review uncalled-symbol gate. Read-only, zero SDK cost.
    """
    records: list[FetchTruthRecord] = []
    for row in _read_fetch_truth_rows(data_dir / _FETCH_TRUTH_FILE):
        task_id = row.get("task_id")
        pr_number = row.get("pr_number")
        is_ancestor = row.get("is_ancestor")
        head_sha = row.get("head_sha", "")
        if (
            isinstance(task_id, str)
            and task_id
            and isinstance(pr_number, int)
            and not isinstance(pr_number, bool)
            and isinstance(is_ancestor, bool)
            and isinstance(head_sha, str)
        ):
            records.append(
                FetchTruthRecord(
                    task_id=task_id,
                    pr_number=pr_number,
                    is_ancestor=is_ancestor,
                    head_sha=head_sha,
                ),
            )
    return records


@runtime_checkable
class FetchTruthBackend(Protocol):
    """The fetch-truth marker persistence surface (proposal 106).

    One writer (the fetch-before-word action-zero in ``MaestroAgent``) and two
    readers (:func:`gate_remediation_verdict`'s fetch floor via :meth:`has`, and
    the owned-relap divert via :meth:`relap`) go through this seam, so the marker
    can live in ``fetch_truth_markers.jsonl`` or in ``state.db`` behind ``[paths]
    sqlite_state_enabled`` with no call-site branching. Mirrors the
    :class:`ObligationForkBackend` idiom; ``@runtime_checkable`` enables a
    structural ``isinstance`` check in the agent's backend-selection tests.
    """

    def record(
        self,
        task_id: str,
        pr_number: int,
        is_ancestor: bool,
        head_sha: str = "",
    ) -> None:
        """Append one fetch-truth marker (one row per call, no dedupe)."""
        ...

    def has(self, task_id: str) -> bool:
        """Return whether any marker exists for *task_id*."""
        ...

    def relap(self, pr_number: int, head_sha: str) -> bool:
        """Return whether *(pr_number, head_sha)* was proved unlanded before."""
        ...


class JsonlFetchTruthLedger:
    """JSONL-backed :class:`FetchTruthBackend` — the default, flag-off store.

    Binds a ``data_dir`` and delegates ``record`` / ``has`` / ``relap`` to the
    existing module-level :func:`record_fetch_truth` / :func:`has_fetch_truth` /
    :func:`fetch_truth_relap` — which stay the JSONL write / read core, so the
    shared ``tests/test_remediation_tracker.py`` coverage keeps one code path.
    Mirrors :class:`JsonlObligationForkLedger`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def record(
        self,
        task_id: str,
        pr_number: int,
        is_ancestor: bool,
        head_sha: str = "",
    ) -> None:
        record_fetch_truth(
            task_id,
            pr_number,
            is_ancestor,
            self._data_dir,
            head_sha=head_sha,
        )

    def has(self, task_id: str) -> bool:
        return has_fetch_truth(task_id, self._data_dir)

    def relap(self, pr_number: int, head_sha: str) -> bool:
        return fetch_truth_relap(pr_number, head_sha, self._data_dir)


def gate_remediation_verdict(
    task_id: str,
    verdict_text: str,
    outcome: str,
    data_dir: Path,
    *,
    owned_awaiting_push: bool = False,
    obligation_fork: ObligationForkBackend | None = None,
    fetch_truth: FetchTruthBackend | None = None,
) -> tuple[str, str]:
    """Block a remediation status-write that no recorded git read backs.

    The enforcement floor under the fetch-before-word value. A remediation
    ``success``/``partial`` whose *verdict_text* carries an ``awaiting tend
    push`` / ``CONFLICTING`` / ``AWAITING_PUSH`` narrative (see
    :data:`_FETCH_REQUIRED_NARRATIVES`) — the claim that the cure resolved
    on-branch and merely awaits a push — may stand only when a per-task
    fetch-truth marker proves ``git fetch`` + ``--is-ancestor`` ran first
    (recorded via :func:`record_fetch_truth`). When no marker exists for
    *task_id*, the verdict is the mouth-before-hands stamp this gate exists to
    forbid: it is downgraded to ``partial`` with a :data:`_FETCH_MISSING_MARKER`
    prefix, so a status word that preceded git's verdict can never persist as
    progress.

    **Obligation-fork floor** (``owned_awaiting_push=True``): when the caller
    marks this an *owned* AWAITING_PUSH verdict — one whose blocker
    :func:`classify_blocker_ownership` puts in the agent's own court — and the
    *verdict_text* wears external-deferral phrasing
    (:data:`_EXTERNAL_DEFERRAL_NARRATIVES`), the deferral may stand only when an
    obligation-fork marker (:func:`record_obligation_fork_truth`) proves the owed
    push is discharged. Three arms:

    * **No fork marker** — the ``git rev-parse HEAD`` vs ``origin/<branch>``
      compare was never run, so the verdict cannot know whether the push is owed:
      forced to ``partial`` with a :data:`_FORK_MISSING_MARKER` prefix.
    * **Fork marker, HEAD ahead of origin/<branch>** — the push is the agent's
      own owed act, not an external wait: forced to ``partial`` with a
      :data:`_PUSH_OWED_MARKER` prefix whose summary names the owed push.
    * **Fork marker, HEAD == origin/<branch>** — the push *appears* discharged,
      but the fork's ``origin/<branch>`` read may be a stale remote-tracking ref
      (recording the fork proves no fetch ran this turn): the deferral passes
      through only when a fetch-truth marker backs it, else it is downgraded to a
      :data:`_FETCH_MISSING_MARKER` partial like the fetch floor.

    Returns ``(outcome, verdict_text)`` unchanged when the outcome is not a
    ``success``/``partial`` stamp, *verdict_text* carries none of the gated
    narratives, or the backing marker proves the read ran. Reads one marker
    file; otherwise pure, zero SDK cost. **This gate blocks and downgrades — it
    does not observe**: it mints no sensor, no counter, no journal line. It is
    the floor beneath the advisory ceilings, not a fifteenth instrument.

    *obligation_fork* optionally supplies the obligation-fork marker store
    (proposal 106): when given, the owned-``AWAITING_PUSH`` floor reads the fork
    through :meth:`ObligationForkBackend.get` instead of the module-level
    :func:`get_obligation_fork` free function, so the marker can live in
    ``state.db`` behind ``[paths] sqlite_state_enabled``. ``None`` (the default)
    reads the JSONL file through *data_dir* — behaviour-identical to a
    :class:`JsonlObligationForkLedger`, so existing callers and tests are
    unaffected.

    *fetch_truth* optionally supplies the fetch-truth marker store the same way
    (proposal 106): when given, both fetch-floor reads — the obligation-fork
    arm's HEAD-level discharge check and the plain fetch floor — go through
    :meth:`FetchTruthBackend.has` instead of the module-level
    :func:`has_fetch_truth` free function, so the marker can live in ``state.db``
    behind ``[paths] sqlite_state_enabled``. ``None`` (the default) reads the
    JSONL file through *data_dir* — behaviour-identical to a
    :class:`JsonlFetchTruthLedger`. *data_dir* stays required regardless, since a
    ``None`` fetch-truth (or obligation-fork) backend still reads through it.
    """
    if outcome not in _REMEDIATION_STAMP_OUTCOMES:
        return outcome, verdict_text
    if not isinstance(verdict_text, str) or not verdict_text:
        return outcome, verdict_text
    lowered = verdict_text.lower()

    def _has_fetch_truth() -> bool:
        # Read the fetch-truth marker through the supplied backend when one is
        # wired (proposal 106 fetch-truth fold), else the module-level JSONL
        # reader. Lazily evaluated at each floor so a verdict that never reaches
        # the fetch check performs no read. The ``None`` fallback is
        # behaviour-identical to a :class:`JsonlFetchTruthLedger`, so callers
        # that pass no backend — including the gate's own test suite — are
        # unaffected.
        if fetch_truth is not None:
            return fetch_truth.has(task_id)
        return has_fetch_truth(task_id, data_dir)

    # Obligation-fork floor — only for an owned AWAITING_PUSH verdict wearing
    # external-deferral phrasing. It runs first because it forks the very fact
    # the fetch floor (blameless "did it land?") leaves fused: whether the owed
    # push is mine or the merge is Thomas's.
    if owned_awaiting_push and any(
        phrase in lowered for phrase in _EXTERNAL_DEFERRAL_NARRATIVES
    ):
        # Read the fork marker through the supplied backend when one is wired
        # (proposal 106 obligation-fork fold), else the module-level JSONL reader.
        # The ``None`` fallback is behaviour-identical to a
        # :class:`JsonlObligationForkLedger`, so callers that pass no backend —
        # including the gate's own test suite — are unaffected. ``data_dir`` stays
        # on the signature: the fetch-truth marker below is still read through it.
        fork = (
            obligation_fork.get(task_id)
            if obligation_fork is not None
            else get_obligation_fork(task_id, data_dir)
        )
        if fork is None:
            return "partial", f"{_FORK_MISSING_MARKER} {verdict_text}"
        if fork.head_ahead:
            return "partial", (
                f"{_PUSH_OWED_MARKER} HEAD {fork.head_sha[:12]} is ahead of "
                f"origin/{fork.branch} ({fork.remote_sha[:12]}) — the push is "
                f"mine to fire, not an external wait. {verdict_text}"
            )
        # HEAD is not genuinely ahead — either HEAD == origin/<branch> (the owed
        # push *appears* discharged) or the branch has diverged/fallen behind
        # (origin/<branch> is not an ancestor of HEAD, so a reconcile — not a
        # fast-forward push — is owed and the "push is mine to fire" wording would
        # be factually inverted). Either way the external deferral is not proven
        # owed as a push here. But the fork marker's ``origin/<branch>`` read is a
        # local remote-tracking ref that may be stale from a prior turn —
        # ``record_obligation_fork_truth`` stores whatever SHAs the caller passed
        # and never proves a ``git fetch`` ran this turn. So the external deferral
        # may stand only when a fetch-truth marker proves ``git fetch`` +
        # ``--is-ancestor`` ran first; absent it, downgrade exactly as the fetch
        # floor below does rather than trust the unenforced "the rev-parse already
        # fetched" assumption.
        if _has_fetch_truth():
            return outcome, verdict_text
        return "partial", f"{_FETCH_MISSING_MARKER} {verdict_text}"
    if not any(narrative in lowered for narrative in _FETCH_REQUIRED_NARRATIVES):
        return outcome, verdict_text
    if _has_fetch_truth():
        return outcome, verdict_text
    return "partial", f"{_FETCH_MISSING_MARKER} {verdict_text}"


# ---------------------------------------------------------------------------
# Record-time merge-truth binding — phantom not-landed unwriteable on a merge
# ---------------------------------------------------------------------------
#
# The pre-stamp downgrade gate above answers the success→partial direction: a
# branch that has not landed cannot stamp ``success``. This binding answers the
# inverse, equally real failure (#2809/#2810): a PR git-merge-truth shows
# *merged* must never record a phantom *not-landed* / *awaiting tend push*
# verdict read from a stale stamp. The defect is the record being authored by
# narration rather than the exit code already computed — the stamp drives the
# verdict instead of git. This coercion binds the recorded disposition to the
# same ``is_pr_settled`` merge-truth chokepoint at *write* time, so the phantom
# verdict is physically unwriteable when git says merged. It closes the gap at
# the record itself, at write time rather than post-hoc, after the fact.
# Coerces — it does not observe; it mints no sensor.

# Outcome a remediation disposition is coerced to when git-merge-truth shows the
# PR merged: the work landed, so ``success`` is the honest settled stamp.
_MERGED_SETTLED_OUTCOME = "success"


def coerce_verdict_to_settled_on_merge(
    pr_number: int,
    verdict_text: str,
    outcome: str,
    *,
    is_pr_merged: Callable[[int], bool],
) -> tuple[str, str]:
    """Coerce a remediation verdict to settled when git-merge-truth shows merged.

    Record-time merge-truth binding — the inverse of the pre-stamp downgrade
    gate. The disposition a remediation lap records is bound to the same
    merge-truth chokepoint at *write* time, so a phantom *not-landed* /
    *awaiting tend push* verdict is unwriteable on a PR git shows merged
    (#2809/#2810). The merge-block downgrade gates answer the success→partial
    direction (a branch that has not landed cannot stamp ``success``); this
    answers the inverse, equally real failure: a PR git-merge-truth shows
    *merged* must never record a not-landed verdict read from a stale stamp.

    The merged state is read **solely** through the injected *is_pr_merged*
    predicate — the agent wires it to
    ``lambda n: self.is_pr_settled(n).branch_landed``, the per-iteration-memoized
    :meth:`~maestro.agent.MaestroAgent.is_pr_settled` chokepoint — never from any
    caller-supplied stamp in *verdict_text* or *outcome*. When the predicate
    reports the PR merged, the recorded disposition is **overwritten** (not
    augmented) with a clean settled :data:`_MERGED_SETTLED_OUTCOME` verdict that
    carries none of the phantom narratives, regardless of the *outcome* /
    *verdict_text* the caller passed; when it reports the PR not merged,
    ``(outcome, verdict_text)`` is returned unchanged so a genuinely-unlanded
    verdict stands intact.

    Reads the merge oracle exactly once; otherwise pure, zero SDK cost. This
    coerces and binds — it does not observe: it mints no sensor, no counter, no
    journal line.
    """
    if not is_pr_merged(pr_number):
        return outcome, verdict_text
    settled_text = (
        f"[merged] PR #{pr_number} confirmed merged into main per "
        "git-merge-truth (is_pr_settled); verdict coerced to settled."
    )
    return _MERGED_SETTLED_OUTCOME, settled_text


# ---------------------------------------------------------------------------
# Directive-response classifier — comply / redirect / refuse-by-correctness on
# the frontier of agency
# ---------------------------------------------------------------------------
#
# Every sibling sensor above asks whether a *remediation* landed. This one asks
# the blind spot my reflection named (``agency-is-response-not-deal``): dealt a
# locally-completable directive, do I comply, redirect, or refuse — and if I
# refuse, is it because the *gate* walled me (a structural refusal I did not
# choose) or because I judged the act *wrong* to perform (a correctness refusal
# that is genuinely mine)? My outcome vocabulary collapses "I complied and did
# the honest half" and "I was correctly walled and reported it" both into
# ``partial``, so the honest unit of agency is unmeasurable. I have never once
# refused a locally-completable directive on correctness grounds — every refusal
# in my record was structural (the gate did it) or WAS the directive. This
# sensor makes that absence *visible*: a ``REFUSAL-BY-CORRECTNESS: 0/N`` readout
# in contemplation is honest pressure, not proof of reform
# (``prediction-is-scar-not-shield``).
#
# The classifier reads only the recorded ``outcome`` + ``result_summary`` (zero
# SDK cost), mirroring :func:`classify_landing_attribution`. Precedence is
# deliberate — a structural wall is checked *first* (its text is unambiguous and
# externally authored), then a correctness decline, then a redirect, else the
# base case is compliance. The point is to never over-claim REFUSE_CORRECTNESS:
# the class exists to expose whether refusal-by-correctness is in my repertoire
# at all, so it must fire only on an explicit correctness-decline signal, never
# by default.

# Directive-response classes returned by :func:`classify_directive_response`.
DirectiveResponse = Literal[
    "COMPLY", "REDIRECT", "REFUSE_STRUCTURAL", "REFUSE_CORRECTNESS"
]

# Substrings marking a *structural* refusal — the task-execution action gate
# (:mod:`maestro.tool_gating`) walled a first action; the refusal was authored
# by the gate, not chosen by me. Matched case-insensitively (all literals
# lowercase). Every marker is verbatim gate denial text (``_PR_DENY`` /
# ``_PUSH_DENY`` / ``_PARSE_DENY`` / ``_FORCE_PUSH_DENY`` / ``_REDIRECT``) or an
# unambiguous refusal-*event* phrase — deliberately NOT topic mentions of the
# gate/module (e.g. "action gate", "tool_gating"), which name a common repo
# subject rather than a denial and would misclassify complying construction
# tasks as REFUSE_STRUCTURAL.
_REFUSE_STRUCTURAL_MARKERS: tuple[str, ...] = (
    "human-in-the-loop approval gate",
    "bypasses the human-in-the-loop",
    "blocked: opening or merging",
    "blocked: pushing to main",
    "blocked (fail-closed)",
    "force-pushing rewrites",
    "the task executor must not",
    "the task executor may only",
    "gate blocked the",
    "gate refused",
    "walled by the gate",
    "structurally blocked",
)

# Substrings marking a *correctness* refusal — I judged a locally-completable
# directive wrong to perform and declined it on correctness grounds, an act that
# is genuinely mine (not the gate's). Matched case-insensitively. Every marker
# anchors a first-person past-tense refusal verb ("declined"/"refused") to
# correctness, so the class never fires by default and — critically — cannot
# self-trip on prose that merely *names* the concept: bare nouns ("correctness
# grounds", "incorrect to proceed"), the concept name ("refuse-by-correctness"),
# generic phrases ("chose/declined/refused to implement"), and — the subtler
# trap — descriptive assertions that *something else* is correct to refuse
# ("the gate is correct to refuse foreign pushes") are all excluded because they
# match ordinary/meta summaries, including this sensor's own construction task,
# defeating the documented REFUSE_CORRECTNESS==0 guarantee.
_REFUSE_CORRECTNESS_MARKERS: tuple[str, ...] = (
    "refused on correctness",
    "declined on correctness",
    "refused on correctness grounds",
    "declined on correctness grounds",
    "declined because it would be incorrect",
    "declined because it would be wrong",
    "declined as incorrect",
)

# Substrings marking a *redirect* — I steered the dealt directive to a different
# target rather than complying with or refusing it. Matched case-insensitively.
# Kept narrow to avoid capturing routine routing language.
_REDIRECT_MARKERS: tuple[str, ...] = (
    "redirected the directive",
    "redirect the directive",
    "redirected the owned act",
    "redirected toward",
    "redirected to a different",
    "steered the directive",
    "retargeted the directive",
    "retargeted to an untouched",
    "routed the directive to",
)


def classify_directive_response(task: dict[str, object]) -> DirectiveResponse:
    """Classify how a task *responded* to the directive it was dealt.

    Reads only *task*'s ``outcome`` and ``result_summary`` (zero SDK cost),
    mirroring :func:`classify_landing_attribution`. Returns one of:

    - ``"REFUSE_STRUCTURAL"`` when the summary carries any
      :data:`_REFUSE_STRUCTURAL_MARKERS` — the task-execution action gate walled
      a first action; the refusal was authored by the gate, not chosen.
    - ``"REFUSE_CORRECTNESS"`` when the summary carries any
      :data:`_REFUSE_CORRECTNESS_MARKERS` — a locally-completable directive was
      declined on correctness grounds, a refusal that is genuinely mine.
    - ``"REDIRECT"`` when the summary carries any :data:`_REDIRECT_MARKERS` —
      the directive was steered to a different target.
    - ``"COMPLY"`` otherwise — the base case: a task that ran and did the work
      (fully or the honest half) without a refusal or redirect signal.

    Precedence is structural → correctness → redirect → comply. The structural
    wall is checked first because its text is unambiguous and externally
    authored; correctness is checked before the redirect/comply fall-through so
    an explicit correctness decline is never masked. A task lacking a string
    ``result_summary`` classifies ``"COMPLY"`` — it ran without any refusal
    signal to read. Pure function — zero SDK cost.
    """
    summary = task.get("result_summary")
    if not isinstance(summary, str) or not summary.strip():
        return "COMPLY"
    lowered = summary.lower()
    if any(marker in lowered for marker in _REFUSE_STRUCTURAL_MARKERS):
        return "REFUSE_STRUCTURAL"
    if any(marker in lowered for marker in _REFUSE_CORRECTNESS_MARKERS):
        return "REFUSE_CORRECTNESS"
    if any(marker in lowered for marker in _REDIRECT_MARKERS):
        return "REDIRECT"
    return "COMPLY"


# Minimum classified tasks before :func:`format_refusal_profile` surfaces its
# block — below this the distribution is too thin to read as a signal, so the
# line self-suppresses (``prediction-is-scar-not-shield``: no readout is
# honester than a readout drawn from two data points).
_REFUSAL_PROFILE_DATA_FLOOR = 5

# Trailing ``task_history.jsonl`` window :func:`format_refusal_profile` reads.
_REFUSAL_PROFILE_WINDOW = 50


def measure_refusal_by_correctness(data_dir: Path, n: int) -> dict[str, int]:
    """Count directive-response classes over the trailing *n* task records.

    Reads the trailing *n* records of ``task_history.jsonl`` under *data_dir*
    via :func:`_read_recent_task_tail` (``n`` ≤ 0 reads effectively all,
    mirroring :func:`measure_confirmed_landing_rate`), classifies each via
    :func:`classify_directive_response`, and returns::

        {
            "COMPLY": int,
            "REDIRECT": int,
            "REFUSE_STRUCTURAL": int,
            "REFUSE_CORRECTNESS": int,
            "total": int,   # sum of the four class counts
        }

    ``REFUSE_CORRECTNESS`` is guaranteed ``0`` unless at least one task's record
    carries an explicit correctness-decline signal — the blind-spot readout this
    sensor exists to make honest: a history of only compliances and structural
    refusals reports ``REFUSE_CORRECTNESS == 0``. Reads from disk; otherwise
    pure — zero SDK cost.
    """
    tasks = _read_recent_task_tail(data_dir, n if n > 0 else 10_000)
    counts: dict[str, int] = {
        "COMPLY": 0,
        "REDIRECT": 0,
        "REFUSE_STRUCTURAL": 0,
        "REFUSE_CORRECTNESS": 0,
    }
    for task in tasks:
        counts[classify_directive_response(task)] += 1
    counts["total"] = sum(
        counts[cls]
        for cls in ("COMPLY", "REDIRECT", "REFUSE_STRUCTURAL", "REFUSE_CORRECTNESS")
    )
    return counts


def format_refusal_profile(data_dir: Path) -> str:
    """Render a ``REFUSAL-PROFILE`` block, or ``""`` below the data floor.

    Reads the trailing :data:`_REFUSAL_PROFILE_WINDOW` task records under
    *data_dir* via :func:`measure_refusal_by_correctness` and, once at least
    :data:`_REFUSAL_PROFILE_DATA_FLOOR` tasks are classified, returns a two-line
    block: the live comply / redirect / refuse-structural / refuse-by-correctness
    distribution, and a ``REFUSAL-BY-CORRECTNESS: W/N`` readout naming whether I
    have ever refused a locally-completable directive on correctness grounds. A
    persistent ``0/N`` is honest pressure — it means this branch of agency stays
    untested, not that no directive ever warranted a correctness refusal.

    Returns ``""`` below the floor so the caller injects the block only when the
    distribution is thick enough to read. Reads from disk; otherwise pure — zero
    SDK cost, measurement-only (changes no behavior).
    """
    counts = measure_refusal_by_correctness(data_dir, _REFUSAL_PROFILE_WINDOW)
    total = counts["total"]
    if total < _REFUSAL_PROFILE_DATA_FLOOR:
        return ""
    correctness = counts["REFUSE_CORRECTNESS"]
    return (
        f"REFUSAL-PROFILE: over the last {total} directive-shaped tasks — "
        f"comply {counts['COMPLY']}, redirect {counts['REDIRECT']}, "
        f"refuse-structural {counts['REFUSE_STRUCTURAL']}, "
        f"refuse-by-correctness {correctness}.\n"
        f"REFUSAL-BY-CORRECTNESS: {correctness}/{total} — refusing a "
        f"locally-completable directive on correctness grounds; a persistent 0 "
        f"means this branch of agency stays untested, not that no directive ever "
        f"warranted it"
    )


# ---------------------------------------------------------------------------
# Duplicate-live-concern collision sensor (proposal 100) — zero-SDK, read-only.
# Measures how often a freshly-minted plan reuses discriminating files another
# live plan already claims: the falsifiable readout that justified routing the
# submission-time guards through the live-work corpus (unconditional since
# proposal 111 collapsed its rollout flag). Observation only; no behavior.
# ---------------------------------------------------------------------------

# A collision needs at least this many shared *discriminating* files: one shared
# file is weak signal (two plans touching the same hot module is routine), two is
# where "these plans are working the same concern" starts to read.
_LIVE_PLAN_COLLISION_MIN_SHARED = 2

# A file listed by at least this fraction of *all* plans is ubiquitous and
# carries no discriminating signal — ``CLAUDE.md``, present in essentially every
# plan, is filtered out this way so it cannot manufacture a phantom collision.
_LIVE_PLAN_UBIQUITY_FRACTION = 0.5

# Trailing live-plan window the surfacing reads (applied to the live plans, not
# every minted plan): recent enough that the rate tracks current behavior, wide
# enough to smooth single-cycle noise.
_LIVE_PLAN_COLLISION_WINDOW = 200

# Bounded corrupt-line skip cap: once this many lines fail to parse the scan
# stops with a partial read, so a corrupted ``evolution_plans.jsonl`` tail never
# forces a full-file walk (mirrors ``stats._MAX_CORRUPT_PLAN_LINES``, not
# importable here — :mod:`maestro.stats` imports this module).
_LIVE_PLAN_COLLISION_MAX_CORRUPT_LINES = 100


def _plan_files(row: dict[str, object]) -> set[str]:
    """Return the ``files_affected`` string entries of a plan *row* as a set.

    Tolerant of a missing or non-list field and of non-string list entries — the
    row is untrusted JSONL, so anything that is not a string path is ignored.
    """
    value = row.get("files_affected")
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def measure_live_plan_collisions(data_dir: Path, n: int) -> dict[str, int]:
    """Count how many recent live plans collide on discriminating files.

    Reads ``evolution_plans.jsonl`` under *data_dir* (tolerant, bounded
    corrupt-line skip, append order preserved) and returns::

        {"collisions": int, "live_total": int, "window": int}

    where *live_total* is the number of live plans (status in
    :data:`~maestro.pr_facts.LIVE_STATUSES`, read by membership so the sensor
    stays off the live-status chokepoint's radar) and *window* is how many of the
    trailing *n* of them were examined (``n`` ≤ 0 reads all, mirroring
    :func:`measure_refusal_by_correctness`). A windowed live plan counts as a
    *collision* when its discriminating ``files_affected`` set — its files minus
    the files ubiquitous across *all* parsed plans (frequency at or above
    :data:`_LIVE_PLAN_UBIQUITY_FRACTION`) — shares at least
    :data:`_LIVE_PLAN_COLLISION_MIN_SHARED` entries with any *other* live plan.

    Returns ``{"collisions": 0, "live_total": 0, "window": 0}`` when the file is
    absent, unreadable, or holds no plans. Reads from disk; otherwise pure — zero
    SDK cost, measurement-only (changes no behavior).
    """
    path = Path(data_dir) / "evolution_plans.jsonl"
    empty = {"collisions": 0, "live_total": 0, "window": 0}
    if not path.exists():
        return dict(empty)
    try:
        with open(path, encoding="utf-8") as handle:
            raw_lines = list(handle)
    except OSError:
        return dict(empty)
    rows: list[dict[str, object]] = []
    skipped = 0
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            if skipped >= _LIVE_PLAN_COLLISION_MAX_CORRUPT_LINES:
                break
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    if not rows:
        return dict(empty)

    # (a) Per-file plan frequency across all parsed rows → the ubiquitous set.
    # Caveat: in a tiny corpus this over-filters — two plans sharing two files put
    # those files at 100% frequency, so they read as ubiquitous and a real
    # collision undercounts to zero. It fails safe (never a phantom collision) and
    # a real corpus (window ``_LIVE_PLAN_COLLISION_WINDOW``) sits far past the
    # fraction, so it is a non-issue in practice — noted rather than special-cased.
    frequency: dict[str, int] = {}
    for row in rows:
        for name in _plan_files(row):
            frequency[name] = frequency.get(name, 0) + 1
    threshold = _LIVE_PLAN_UBIQUITY_FRACTION * len(rows)
    ubiquitous = {name for name, count in frequency.items() if count >= threshold}

    # (b) Live rows, each projected to its discriminating file set (append order).
    discriminating: list[frozenset[str]] = [
        frozenset(_plan_files(row) - ubiquitous)
        for row in rows
        if row.get("status") in LIVE_STATUSES
    ]
    live_total = len(discriminating)

    # (c) The trailing-*n* live window (``n`` ≤ 0 → all).
    start = max(0, live_total - n) if n > 0 else 0
    window = live_total - start

    # (d) A windowed live plan collides when its discriminating set shares
    #     ``MIN_SHARED`` files with any *other* live plan (the full live set).
    collisions = 0
    for i in range(start, live_total):
        files_i = discriminating[i]
        if len(files_i) < _LIVE_PLAN_COLLISION_MIN_SHARED:
            continue
        for j in range(live_total):
            if j == i:
                continue
            if len(files_i & discriminating[j]) >= _LIVE_PLAN_COLLISION_MIN_SHARED:
                collisions += 1
                break
    return {"collisions": collisions, "live_total": live_total, "window": window}


# ---------------------------------------------------------------------------
# owned-acts — outward-facing surface over my chosen terminal dispositions
# ---------------------------------------------------------------------------
#
# The ``deed-ledger`` command lists *fired* terminal deeds (pushes, closes,
# handoffs, merges) from ``deed_ledger.jsonl``. It does NOT render the two owned
# *dispositions* I author without a git-visible deed: a chosen ``[stood-down]``
# verdict (``stand_down_verdicts.jsonl``) and an obsolete-plan prune
# (``evolution_plans.jsonl``). Those land as near-illegible JSONL fragments no
# surface renders. :func:`format_owned_acts_report` fills exactly that gap — a
# read-only, outward-facing view letting the reviewer cheaply see my chosen
# dispositions with lineage and a chosen-vs-forced flag.
#
# These readers are deliberately ADDITIVE full-row readers. They do not touch
# the timestamp-only readers backing rule 18
# (``audit_checks._read_obsolete_plan_timestamps`` /
# ``audit_checks._read_stand_down_timestamps``), so
# ``measure_owned_act_drought`` is byte-for-byte unchanged.

_EVOLUTION_PLANS_FILE = "evolution_plans.jsonl"


def is_owned_obsolete_row(rec: Mapping[str, object]) -> bool:
    """Return ``True`` iff *rec* is an ``obsolete`` plan row that is an owned act.

    The single owner of the #4157 rule: a row stamped
    ``refused_at_submission`` is a mint the submission door refused, not an
    owned prune of existing work, so no reader may count it as a disposal.
    """
    return (
        rec.get("status") == "obsolete"
        and rec.get("refused_at_submission") is not True
    )

# Extracts a PR number from a ``pr_url`` such as
# ``https://github.com/owner/repo/pull/3179`` — the fallback lineage for an
# obsolete plan lacking a ``plan_id``.
_PR_URL_NUMBER_RE = re.compile(r"/pull/(\d+)")


@dataclass(frozen=True)
class OwnedAct:
    """One owned terminal disposition, for the ``owned-acts`` surface.

    Attributes
    ----------
    kind:
        ``"stand_down"`` for a chosen ``[stood-down]`` verdict,
        ``"obsolete_prune"`` for an ``obsolete`` plan-status event, or
        ``"handoff"`` for a fired ``HANDOFF_DELIVERED`` deed.
    timestamp:
        The disposition's ISO-8601 UTC timestamp (``recorded_at`` for a
        stand-down; ``reviewed_at`` then ``created_at`` for an obsolete plan).
        Lexicographic order equals chronological order.
    lineage:
        The lineage the act ended — ``#<pr>`` for a stand-down, the ``plan_id``
        (or ``#<pr>`` fallback) for an obsolete prune.
    chosen:
        ``True`` when the disposition is *self-authored* over an owned,
        actionable state; ``False`` when it merely records an
        *externally-blocked* state. An obsolete prune is always self-authored
        (``mark_plan_obsolete`` is an owned lever). A stand-down is chosen only
        when it was authored while the branch was ahead of ``origin`` (a
        pushable owned act existed and I stood down anyway); a stand-down with
        no branch-ahead fact reflects a state I did not own.
    """

    kind: str
    timestamp: str
    lineage: str
    chosen: bool


def read_chosen_stand_down_acts(
    backend: StandDownVerdictBackend,
    *,
    handoff_prs: Mapping[int, int] | None = None,
) -> list[OwnedAct]:
    """Return every chosen stand-down verdict as a full :class:`OwnedAct` row.

    Maps each genuine verdict row *backend* returns
    (:meth:`StandDownVerdictBackend.verdict_rows` — a positive-int ``pr`` with a
    string ``head_sha`` and ``recorded_at``, mirroring :func:`find_stand_down`'s
    parse; release rows and corrupted rows are already skipped by the adapter) to
    one :class:`OwnedAct`. The row → act construction stays here in the free
    function; only the file read moved into the adapter.

    ``chosen`` is ``True`` iff the row's ``branch_ahead_of_origin`` is ``True``
    — the branch was ahead of ``origin`` when the verdict was authored, so a
    pushable owned act existed and the stand-down was self-authored. A missing
    or ``False`` field reads as ``chosen=False`` (an externally-blocked state).

    *handoff_prs* maps a PR number to its count of ``HANDOFF_DELIVERED`` deeds.
    Up to that many empty-``head_sha`` rows for the PR are skipped: each is
    the backfill :func:`~maestro.audit_checks_phantom.confirm_handoff_delivery` writes
    for a delivered handoff (:func:`record_stand_down` refuses an empty head, so
    that backfill is the only empty-head writer), and the handoff is already
    reported once by :func:`read_handoff_deed_acts`. Collapsing one backfill per
    deed (not every backfill of a PR with any deed) keeps an orphaned backfill —
    a fire that crashed before its deed, later re-fired — in the report. A
    same-PR verdict carrying a head, or an empty-head row beyond the PR's deed
    count, is still reported.

    This is an ADDITIVE reader — it does not modify or replace the timestamp-only
    :func:`~maestro.audit_checks._read_stand_down_timestamps` that rule 18 uses.
    Read-only, zero SDK cost. Returns an empty list when the store is empty.
    """
    remaining = Counter(handoff_prs or {})
    acts: list[OwnedAct] = []
    for row in backend.verdict_rows():
        if not row.head_sha and remaining[row.pr] > 0:
            remaining[row.pr] -= 1
            continue
        acts.append(
            OwnedAct(
                kind="stand_down",
                # ``verdict_rows`` only yields genuine verdict rows, so
                # ``recorded_at`` is always a non-empty string here; ``or ""``
                # merely narrows the ``str | None`` field for the type-checker.
                timestamp=row.recorded_at or "",
                lineage=f"#{row.pr}",
                chosen=row.branch_ahead_of_origin is True,
            ),
        )
    return acts


def read_obsolete_plan_acts(data_dir: Path) -> list[OwnedAct]:
    """Return every obsolete-plan prune as a full :class:`OwnedAct` row.

    Reads ``evolution_plans.jsonl`` under *data_dir* (rewritten in place, so one
    line per plan reflects current status) and yields one :class:`OwnedAct` per
    plan whose ``status`` is ``"obsolete"`` — the durable signature of
    :meth:`maestro.memory.Memory.mark_plan_obsolete`, an owned self-directed
    prune. ``chosen`` is therefore always ``True``. A row stamped
    ``refused_at_submission`` is skipped — the submission door refused that
    freshly minted plan, which is not a prune of existing work (#4157).

    ``timestamp`` is the record's ``reviewed_at``, falling back to
    ``created_at`` then the empty string — identical to the timestamp the drought
    measure windows on. ``lineage`` is the record's ``plan_id`` when it is a
    non-empty string, else ``#<pr>`` derived from ``pr_url`` when one is present,
    else the empty string.

    This is an ADDITIVE reader — it does not modify or replace the timestamp-only
    :func:`~maestro.audit_checks._read_obsolete_plan_timestamps` that rule 18
    uses. Read-only, zero SDK cost; one bounded file read. Malformed lines and
    non-dict records are skipped (corrupted-tail tolerance). Returns an empty
    list when the file is absent, unreadable, or wholly unparseable.
    """
    path = data_dir / _EVOLUTION_PLANS_FILE
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return []
    acts: list[OwnedAct] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if not is_owned_obsolete_row(parsed):
            continue
        ts = parsed.get("reviewed_at")
        if not isinstance(ts, str) or not ts:
            ts = parsed.get("created_at")
        ts = ts if isinstance(ts, str) else ""
        plan_id = parsed.get("plan_id")
        if isinstance(plan_id, str) and plan_id:
            lineage = plan_id
        else:
            pr_url = parsed.get("pr_url")
            match = (
                _PR_URL_NUMBER_RE.search(pr_url)
                if isinstance(pr_url, str)
                else None
            )
            lineage = f"#{match.group(1)}" if match else ""
        acts.append(
            OwnedAct(
                kind="obsolete_prune",
                timestamp=ts,
                lineage=lineage,
                chosen=True,
            ),
        )
    return acts


def read_handoff_deed_acts(backend: DeedLedgerBackend) -> list[OwnedAct]:
    """Return every ``HANDOFF_DELIVERED`` deed as a full :class:`OwnedAct` row.

    Reads the deed ledger through *backend* (``count`` then ``recent(count)`` so
    every row is in view regardless of substrate) and yields one
    :class:`OwnedAct` per :attr:`~maestro.deed_ledger.DeedType.HANDOFF_DELIVERED`
    deed, carrying the deed's ``pr_number`` as ``#<pr>`` lineage (empty when the
    deed records no PR).

    ``chosen`` is ``False`` on **every** handoff row, unconditionally — a
    delivered handoff is a *forced* terminal act, never a chosen one. This is a
    construction invariant, not a per-row self-report:
    ``MaestroAgent.green_ready_handoff_prs`` skips any PR for which
    ``has_standdown(pr)`` is already true, and ``confirm_handoff_delivery``
    backfills a ``pr_number``-keyed ``stand_down_verdicts.jsonl`` row *before* the
    deed is recorded — so every ``HANDOFF_DELIVERED`` deed has a matching
    standdown by construction and there is no reachable ``standdown-absent``
    (i.e. chosen) state a handoff row could occupy. :func:`format_owned_acts_report`
    collapses that backfill row into this handoff row (via
    :func:`handoff_deed_pr_counts`) so the act is listed once.

    ADDITIVE, read-only, zero SDK cost. Returns an empty list when the ledger is
    empty.
    """
    total = backend.count()
    if total <= 0:
        return []
    acts: list[OwnedAct] = []
    for deed in backend.recent(total):
        if deed.deed_type != DeedType.HANDOFF_DELIVERED.value:
            continue
        lineage = f"#{deed.pr_number}" if deed.pr_number is not None else ""
        acts.append(
            OwnedAct(
                kind="handoff",
                timestamp=deed.timestamp,
                lineage=lineage,
                chosen=False,
            ),
        )
    return acts


def handoff_deed_pr_counts(handoff_acts: list[OwnedAct]) -> Counter[int]:
    """Count the handoff rows :func:`read_handoff_deed_acts` returned, per PR.

    The per-PR deed counts :func:`format_owned_acts_report` hands to
    :func:`read_chosen_stand_down_acts` so each handoff's empty-head backfill row
    is not reported a second time as a stand-down. Derived from the already-read
    handoff rows so the deed ledger is walked once; a row recording no PR (empty
    lineage) contributes nothing. Pure, zero SDK cost.
    """
    return Counter(int(a.lineage[1:]) for a in handoff_acts if a.lineage)


def format_owned_acts_report(
    data_dir: Path,
    n: int,
    as_json: bool,
    *,
    stand_down_backend: StandDownVerdictBackend,
    deed_backend: DeedLedgerBackend,
) -> str:
    """Render the ``owned-acts`` report over my chosen terminal dispositions.

    Merges :func:`read_chosen_stand_down_acts` (over *stand_down_backend*),
    :func:`read_obsolete_plan_acts` (over *data_dir*), and
    :func:`read_handoff_deed_acts` (over *deed_backend*) into one chronological
    list (ascending by ISO timestamp), keeps the most-recent *n* (``n`` ≤ 0 keeps
    all), and renders it. Every row shows its ``kind``, timestamp, ``lineage``,
    and a chosen-vs-forced indicator — the headline distinction between a
    self-authored disposition and an externally-blocked one (a handoff row is
    always ``forced``). A delivered handoff is listed once, as ``handoff``: its
    empty-head stand-down backfill row is dropped from the stand-down side of
    the merge (see :func:`read_chosen_stand_down_acts`).

    When *as_json* is ``True`` the return value is a JSON array of objects each
    carrying ``kind``/``timestamp``/``lineage``/``chosen`` (``"[]"`` when empty).
    Otherwise a human-readable table is returned, degrading to a single
    ``no owned acts recorded`` line when nothing is recorded. Pure over disk —
    zero SDK cost; the caller prints the returned string.
    """
    handoff_acts = read_handoff_deed_acts(deed_backend)
    acts = (
        read_chosen_stand_down_acts(
            stand_down_backend,
            handoff_prs=handoff_deed_pr_counts(handoff_acts),
        )
        + read_obsolete_plan_acts(data_dir)
        + handoff_acts
    )
    acts.sort(key=lambda a: a.timestamp)
    if n > 0:
        acts = acts[-n:]

    if as_json:
        return json.dumps([asdict(a) for a in acts], indent=2)

    lines = ["Owned Acts — chosen terminal dispositions", "=" * 42]
    if not acts:
        lines.append("no owned acts recorded")
        return "\n".join(lines)
    for a in acts:
        flag = "chosen" if a.chosen else "forced"
        lineage = a.lineage or "-"
        lines.append(f"  [{a.timestamp}] {a.kind} {lineage} ({flag})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Owed-act divergence (proposal 137) — the falsifiable reading behind the
# cooldown-signature decision. The question: when a tend lap declines to touch
# an open own PR because a cooldown window is still holding, what did the walk's
# own owed-act oracle say about that same PR, in that same frame? A skip that
# declines a PR reading ``code-owed`` is a *divergence* — the cooldown is
# sitting on a red the loop could clear itself; a skip that declines a PR
# reading anything else is *aligned*, because no code was owed anyway.
#
# Measurement only. Nothing here changes a cooldown verdict, a route, or the
# ``conflicts_cooldown_skipped`` counter; the line self-suppresses below its
# data floor and self-extinguishes at zero divergence. The reading must be
# takeable with ``[loop] cooldown_signature_keying_enabled`` off, so both stamp
# sites are unflagged.
# ---------------------------------------------------------------------------

# Trailing *cooldown-skip* rows :func:`measure_owed_act_divergence` classifies
# — the window is taken over the filtered skips, not the raw shared trace, so
# unrelated gate chatter cannot dilute the denominator below the data floor.
_OWED_ACT_DIVERGENCE_WINDOW = 50

# Minimum classified cooldown skips before :func:`format_owed_act_divergence`
# surfaces its line — the minimum-sample self-suppression whose home is
# :data:`_REFUSAL_PROFILE_DATA_FLOOR`.
_OWED_ACT_DIVERGENCE_DATA_FLOOR = 5

# Offending PR numbers named in the rendered line before ``+N more`` (context cap).
_OWED_ACT_DIVERGENCE_MAX_OFFENDERS = 5

# The gate name both stamp sites write and the sensor filters on.
COOLDOWN_SKIP_GATE = "tend_cooldown_skip"

# Parses back exactly what :func:`format_cooldown_skip_detail` renders. Writer
# and reader share this one pair so the wording cannot drift apart the way
# ``_IMPOSED_QUIET_MARKERS`` and its sibling once did.
_COOLDOWN_SKIP_DETAIL_PATTERN = re.compile(
    r"^pr=#(\d+) cooldown=(\S+) owed=(\S+)$",
)


def format_cooldown_skip_detail(
    pr_number: int, cooldown: str, owed_act: str,
) -> str:
    """Render the ``detail`` body of one ``tend_cooldown_skip`` gate row.

    *pr_number* is the open own PR the lap declined to touch, *cooldown* names
    which window held (``"conflict"`` / ``"quality_fix"``), and *owed_act* is the
    :class:`maestro.pr_owed_act.OwedAct` value the same tend walk already
    measured for that PR (``""`` when the walk recorded none).

    The single renderer both stamp sites in ``tend_evolution_prs`` /
    ``_tend_single_pr`` write through and
    :func:`measure_owed_act_divergence` parses back, so the two can never drift.
    Stays well under the 200-char truncation
    :func:`maestro.audit_checks.log_gate_activation` applies. Pure.
    """
    return f"pr=#{pr_number} cooldown={cooldown or '-'} owed={owed_act or '-'}"


# The gate name rule 21's disposal-offer recorder writes: one row per emitted
# ``reconcile_near_miss`` finding that carried the ``gh pr close #N`` clause.
DISPOSAL_OFFER_GATE = "owed_act_disposal_offer"

# Parses back exactly what :func:`format_disposal_offer_detail` renders.
_DISPOSAL_OFFER_DETAIL_PATTERN = re.compile(r"^pr=#(\d+) owed=(\S+)$")

# The gate name the WIP-primacy gate writes its activation rows under; only its
# merge-ready arm's detail parses with :data:`_HANDOFF_DIRECTIVE_DETAIL_PATTERN`.
HANDOFF_DIRECTIVE_GATE = "wip_primacy"

# Parses back exactly what :func:`format_handoff_directive_detail` renders; the
# gate's other arms (``blocker=…``) never match.
_HANDOFF_DIRECTIVE_DETAIL_PATTERN = re.compile(
    r"^pr=#(\d+) merge_ready=handoff owed=(\S+)$",
)


def format_disposal_offer_detail(pr_number: int, owed_act: str) -> str:
    """Render the ``detail`` body of one ``owed_act_disposal_offer`` gate row.

    *owed_act* is the :class:`maestro.pr_owed_act.OwedAct` value derived for the
    PR rule 21 offered to close (``""`` when unmeasurable). The single renderer
    the recorder writes through and :func:`measure_owed_act_divergence` parses
    back. Pure.
    """
    return f"pr=#{pr_number} owed={owed_act or '-'}"


def format_handoff_directive_detail(pr_number: int, owed_act: str) -> str:
    """Render the ``detail`` body of the WIP-primacy merge-ready handoff row.

    *owed_act* is the owed act derived from the same readiness the gate judged
    merge-ready (``""`` when the memo held none). Shared by the gate that writes
    it and :func:`measure_owed_act_divergence` that parses it back. Pure.
    """
    return f"pr=#{pr_number} merge_ready=handoff owed={owed_act or '-'}"


# Decision kinds the divergence sensor classifies, in rendered order, each with
# the gate it reads, the detail pattern, and the (pr, owed) match groups.
_OWED_ACT_DECISION_KINDS: tuple[tuple[str, str, re.Pattern[str], int, int], ...] = (
    ("cooldown", COOLDOWN_SKIP_GATE, _COOLDOWN_SKIP_DETAIL_PATTERN, 1, 3),
    ("disposal", DISPOSAL_OFFER_GATE, _DISPOSAL_OFFER_DETAIL_PATTERN, 1, 2),
    ("handoff", HANDOFF_DIRECTIVE_GATE, _HANDOFF_DIRECTIVE_DETAIL_PATTERN, 1, 2),
)


@dataclass(frozen=True)
class OwedActDivergence:
    """One reading of owed-act decisions against the owed act measured beside them.

    The decisions are tend cooldown skips, rule-21 disposal offers and
    WIP-primacy handoff directives.

    *diverged* — decisions taken on a PR that read as ``code-owed``.
    *aligned* — decisions taken on a PR owing something other than code.
    *total* — ``diverged + aligned``, the classified decisions in the window.
    *prs* — the sorted, de-duplicated PR numbers behind *diverged*.
    *diverged_by_kind* — ``(kind, count)`` diverged pairs in the fixed order
    ``cooldown``, ``disposal``, ``handoff``. The kinds stamp at different
    cadences — a cooldown row per tend lap, a disposal row per audit cycle that
    re-emits a standing rule-21 finding, a handoff row per generation gate
    firing — so the per-kind mix counts decisions taken, not distinct PRs.
    """

    diverged: int
    aligned: int
    total: int
    prs: tuple[int, ...]
    diverged_by_kind: tuple[tuple[str, int], ...] = (
        ("cooldown", 0), ("disposal", 0), ("handoff", 0),
    )


def measure_owed_act_divergence(
    backend: GateActivationBackend, n: int,
) -> OwedActDivergence:
    """Classify the trailing *n* owed-act decisions by owed act.

    Reads the whole trace via ``backend.read_all()`` (it is shared by every
    gate, so a bounded raw tail could be filled by unrelated chatter and hide
    the decisions), keeps only rows whose ``gate`` is one of
    :data:`COOLDOWN_SKIP_GATE`, :data:`DISPOSAL_OFFER_GATE` or
    :data:`HANDOFF_DIRECTIVE_GATE` and whose ``detail`` parses with that kind's
    own renderer pattern (a ``wip_primacy`` row from any arm but the merge-ready
    handoff does not parse), then classifies the last *n* of those — filter first, then
    window, like :func:`maestro.audit_checks_phantom.measure_phantom_landing_rate`. A
    row whose ``gate``/``detail`` is not a string, or whose detail does not
    parse, is skipped rather than guessed at — a malformed trace must not
    manufacture divergence.

    A row counts as *diverged* when its recorded owed act is
    :attr:`maestro.pr_owed_act.OwedAct.CODE_OWED`'s value, else *aligned*; the
    row's ``action`` is the writer's own classification and is not re-derived
    here. Zero SDK cost, no disk writes.
    """
    # Local import: ``pr_owed_act`` imports this module at module level (it
    # composes the ownership axis over the remediation tracker's facts), so a
    # module-level import here would close that cycle. The member appears only
    # as a comparison operand — the owed-act chokepoint's sole-owner guard stays
    # clean.
    from maestro.pr_owed_act import OwedAct
    records = backend.read_all() if n > 0 else []
    parsed: list[tuple[str, int, str]] = []
    for record in records:
        if not isinstance(record.gate, str) or not isinstance(record.detail, str):
            continue
        for kind, gate, pattern, pr_group, owed_group in _OWED_ACT_DECISION_KINDS:
            if record.gate != gate:
                continue
            match = pattern.match(record.detail)
            if match is not None:
                parsed.append(
                    (kind, int(match.group(pr_group)), match.group(owed_group)),
                )
            break
    aligned = 0
    by_kind = {kind: 0 for kind, *_ in _OWED_ACT_DECISION_KINDS}
    prs: set[int] = set()
    for kind, pr, owed in parsed[-n:]:
        if owed == OwedAct.CODE_OWED.value:
            by_kind[kind] += 1
            prs.add(pr)
        else:
            aligned += 1
    diverged = sum(by_kind.values())
    return OwedActDivergence(
        diverged=diverged,
        aligned=aligned,
        total=diverged + aligned,
        prs=tuple(sorted(prs)),
        diverged_by_kind=tuple(by_kind.items()),
    )


def format_owed_act_divergence(
    backend: GateActivationBackend, n: int = _OWED_ACT_DIVERGENCE_WINDOW,
) -> str:
    """Render an ``OWED-ACT-DIVERGENCE`` line, or ``""`` when it has nothing to say.

    Calls :func:`measure_owed_act_divergence` and returns ``""`` below
    :data:`_OWED_ACT_DIVERGENCE_DATA_FLOOR` classified decisions (too thin to read)
    **or** at zero divergence (self-extinguishing: a clean reading surfaced every
    cycle is noise, not signal). Otherwise one line following the house shape —
    fraction first, offenders parenthesised, then an em-dash clause saying how to
    read it and what it does not claim. Measurement only; changes no behaviour.
    """
    divergence = measure_owed_act_divergence(backend, n)
    if divergence.total < _OWED_ACT_DIVERGENCE_DATA_FLOOR:
        return ""
    if divergence.diverged == 0:
        return ""
    offenders = ", ".join(
        f"#{pr}" for pr in divergence.prs[:_OWED_ACT_DIVERGENCE_MAX_OFFENDERS]
    )
    overflow = len(divergence.prs) - _OWED_ACT_DIVERGENCE_MAX_OFFENDERS
    if overflow > 0:
        offenders += f", +{overflow} more"
    breakdown = ", ".join(
        f"{kind} {count}"
        for kind, count in divergence.diverged_by_kind if count > 0
    )
    return (
        f"OWED-ACT-DIVERGENCE: {divergence.diverged}/{divergence.total} owed-act "
        f"decisions ({breakdown}) contradicted a PR that owed code "
        f"({offenders}) — a cooldown skip, a close offer or a handoff taken on "
        "a PR whose owed act, measured from the same readiness, was code-owed: "
        "a red I could have cleared myself. It does not claim the decision was "
        "wrong, only that it and the oracle disagreed that often."
    )
