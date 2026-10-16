"""No-escalation-bridge: turn an unlanded cure into one durable standdown.

``UNLANDED-REMEDIATION #2579 x33`` is the ground truth this module answers:
when a cure is implemented-but-unmerged and only Thomas can land it, the loop
mints remediation lap *N+1* instead of sending one message. Every sibling
sensor in :mod:`maestro.remediation_tracker` *measures* that wheel; none of
them deliver the human knock that would stop it. This module builds the
missing pipe by construction:

* :func:`detect_unlanded_cure_handoff` consumes the same git-merge-truth
  :func:`~maestro.remediation_tracker.measure_unlanded_remediations` sensor and
  returns the oldest unlanded PR whose cumulative lap count has crossed the lap
  budget *and* for which no ``pr_number``-keyed standdown has yet been recorded.
* :func:`deliver_handoff` appends **exactly one** merge-or-close message
  addressed to Thomas to ``messages.jsonl`` and persists a durable
  ``pr_number``-keyed standdown record under
  :func:`~maestro.fileutil.file_lock`. Both happen under the standdown-ledger
  lock, so repeated calls are idempotent: the second call is a no-op.

Proposal 080 Phase 3 re-keys the ledger from ``(pr, head_sha)`` to
``pr_number``. The previous per-head keying re-knocked on every fresh commit to
a still-unmerged PR, so re-resolving a conflict reset the standdown and the
loop never stopped minting laps. A ``pr_number``-keyed standdown sticks: once a
PR is handed up, **no further minting fires for it across any number of head
changes** until the host API reports it merged/closed — at which point
:func:`prune_resolved_standdowns` clears the record so a genuinely new problem
on a reopened PR can earn a fresh knock. The standdown carries the head SHA at
delivery time as an *informational* field only; it no longer gates re-delivery.
Standing down means handing **up** once — not re-minting a remediation lap — so
both the Tend route (:meth:`~maestro.loop.pr_remediation.PrRemediationController.route_feedback`)
and the registered ``escalation_bridge`` task gate suppress remediation while a
standdown is recorded.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    NamedTuple,
    NotRequired,
    Protocol,
    TypedDict,
    TypeVar,
    runtime_checkable,
)

from maestro.fileutil import atomic_write, file_lock
from maestro.messaging import Message, MessageStore
from maestro.pr_owed_act import OwedAct
from maestro.remediation_tracker import (
    MERGE_BLOCK_NONE,
    _read_recent_task_tail,
    measure_unlanded_remediations,
    pr_number_from_remediation_goal,
)
from maestro.standdown_validity import (
    StanddownClaim,
    StanddownValidity,
    assess_standdown_validity,
    parse_standdown_claim,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from maestro.config import Config

logger = logging.getLogger("maestro")

T = TypeVar("T")


class PlanCapability(Protocol):
    """The minimal owning-plan shape :func:`deliver_handoff` reads to name the
    capability a merge would land.

    :class:`~maestro.memory.EvolutionPlan` satisfies it structurally, so callers
    pass the concrete plan (via ``_plan_for_pr_number``) while this leaf module
    avoids importing :mod:`maestro.memory` — which would couple the escalation
    bridge to the plan store. Only the two fields the capability line reads are
    declared.
    """

    title: str
    acceptance_criteria: list[str]

# JSON ledger under ``data_dir`` recording durable per-PR standdowns as
# ``{"<pr>": {"head_sha": "<sha>", "delivered_at": "<iso-timestamp>",
# "serviced_comment_ids": [<int>, ...]}}``. Keyed by ``pr_number`` (proposal 080
# Phase 3) so a fresh commit on a still-unmerged PR no longer re-knocks: exactly
# one handoff is delivered, then minting is suppressed until the host API reports
# the PR merged/closed. ``serviced_comment_ids`` (#2798) tracks the review-thread
# comment ids that have already earned a remediation lap under this standdown, so
# a genuinely-new review thread breaks through for exactly one lap while a
# re-firing already-serviced thread stays suppressed (treadmill guard); it is
# seeded empty at record time and dies with the record on merge/close. The loader
# also accepts the pre-080 ``"<pr>@<head>": "<iso-timestamp>"`` form (with an
# empty serviced set) and treats any such entry as a delivered standdown for that
# ``pr`` so no duplicate knock fires on upgrade.
_DELIVERY_FILE = "escalation_handoffs.json"

# How many success/partial UNLANDED-REMEDIATION laps a PR must accumulate
# before a human handoff is warranted, when a caller does not pass an explicit
# budget. Production callers pass ``[loop] pr_remediation_lap_budget`` (default
# 6); this module-level constant is the fallback for direct callers and tests.
# Above the forced-read bridge's threshold (2): the forced-read directive gets
# first chance to drive the PR to merge; the handoff is the escalation when
# re-lapping has demonstrably not landed it.
HANDOFF_STAMP_THRESHOLD = 3

# Trailing ``task_history.jsonl`` window the detector scans, mirroring the
# UNLANDED-REMEDIATION sensor so a long-abandoned lineage ages out rather than
# nagging forever.
HANDOFF_SCAN_CYCLES = 50

# How long an issue-keyed claimed-escalation standdown (proposal 090) lives
# before :func:`prune_expired_standdowns` clears it. Rule 12 keys on
# :func:`~maestro.audit_checks.extract_issue_numbers`, which can cite a plain
# issue rather than a PR; a standdown keyed on a plain issue can never be
# released by :func:`prune_resolved_standdowns` (an issue never "merges as a
# PR"), so it carries an age-based expiry to keep the ledger bounded. Every
# PR/reality-refutation handoff sets no ``expires_at`` and keeps its
# merge/close-only release.
_CLAIMED_ESCALATION_EXPIRY = timedelta(days=30)


@dataclass(frozen=True)
class UnlandedCureHandoff:
    """One unlanded cure needing a human knock.

    ``pr_number`` is the PR git-merge-truth shows unmerged; ``head_sha`` is its
    most recent recorded remediation head (``""`` when no lap recorded one),
    carried as an *informational* field only — the standdown is keyed on
    ``pr_number`` alone; ``stamp_count`` is the cumulative unlanded lap count.
    """

    pr_number: int
    head_sha: str
    stamp_count: int


class StanddownRecord(TypedDict):
    """One durable per-PR standdown record — the shape both substrates share.

    Public (proposal 106) because the :class:`EscalationLedgerBackend` seam
    traffics whole ``{number: record}`` maps between the module's policy
    functions and the flag-selected store (``escalation_handoffs.json`` via
    :class:`JsonEscalationLedger`, or ``state.db`` via
    :class:`~maestro.state_store.SqliteEscalationLedger`).

    ``head_sha`` is the head SHA at delivery time (informational only — the
    ledger gates on ``pr_number`` presence). ``delivered_at`` is the ISO
    delivery timestamp. ``serviced_comment_ids`` (#2798) holds the review-thread
    comment ids already routed a remediation lap under this standdown — seeded
    empty at record time, grown only via :func:`mark_comment_ids_serviced`, and
    dropped with the whole record on merge/close. ``expires_at`` (proposal 090)
    is an optional ISO deadline: issue-keyed claimed-escalation records carry it
    so :func:`prune_expired_standdowns` can age them out (the merge/close pruner
    can never release a plain issue number); every PR/reality-refutation handoff
    omits it and keeps its merge/close-only release.

    ``claim`` (proposal 135) is an optional
    :class:`~maestro.standdown_validity.StanddownClaim` value naming *the fact
    this record asserts* — each of the four deliverers stamps its own, so a
    later reader can re-ask that specific question instead of assuming every
    latch means "merge-ready". A record written before this key existed loads
    **unclaimed** (the key simply absent), which the validity oracle reads as
    holding; defaulting it to the handoff claim would mis-attribute a
    retirement-disposition or reality-refutation record. No state file is
    rewritten on upgrade.

    ``refutation_releases`` (proposal 135) is an optional count of how many times
    *this PR's* latch has already been released by refutation, stamped by
    :func:`deliver_handoff` from :func:`count_refutation_releases` when it writes
    a successor record. It is a **read-only projection for inspection**, not the
    cap authority: the release deletes the record, so a counter living on it
    would be destroyed by the event it counts — the durable withdrawal-message
    ledger is the authority. A record written before this key existed loads
    without it, and no state file is rewritten on upgrade.
    """

    head_sha: str
    delivered_at: str
    serviced_comment_ids: list[int]
    expires_at: NotRequired[str]
    claim: NotRequired[str]
    refutation_releases: NotRequired[int]


def _standdowns_path(data_dir: Path) -> Path:
    """Return the path to the standdown ledger under *data_dir*."""
    return data_dir / _DELIVERY_FILE


def _require_aware_timestamp(value: str, param: str) -> datetime:
    """Parse *value* as a tz-aware ISO timestamp or raise naming *param*.

    The write-side half of the ledger's tz-awareness invariant: the loader
    (:func:`_parse_standdown_entry`) *drops* a malformed or naive ``expires_at``
    to stay corruption-tolerant, which silently turns a naive write into a
    record no pruner can ever release. Enforcing the contract where the
    timestamp is *established* — an injected ``now`` — makes the violation fail
    loudly, and names the offending parameter rather than surfacing as an
    opaque ``TypeError`` from a downstream aware/naive comparison.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"{param} must be an ISO timestamp, got {value!r}"
        raise ValueError(msg) from exc
    if parsed.tzinfo is None:
        msg = f"{param} must be timezone-aware, got naive {value!r}"
        raise ValueError(msg)
    return parsed


def _parse_standdown_entry(
    raw_key: str, raw_val: object,
) -> tuple[int, StanddownRecord] | None:
    """Parse one persisted ledger entry; ``None`` on a malformed one.

    The per-entry tolerant parse core both substrates read through (proposal
    106): the JSON adapter's whole-file load and the SQLite store's per-row
    blob hydration each hand their raw entries here, so the pre-080
    ``"<pr>@<head>"`` key migration (head recovered from the key suffix), the
    legacy-string-record upgrade, the serviced-set degradation (a missing,
    legacy-string, or non-list ``serviced_comment_ids`` degrades to an empty
    set; non-int elements are dropped), and the malformed/naive ``expires_at``
    drop stay byte-identical across stores. A non-digit key part or a value
    that is neither dict nor string is malformed → ``None``.
    """
    # JSON object keys are always strings, so each key is partitioned on ``@``
    # to recover the PR number (new ``"<pr>"`` form or legacy ``"<pr>@<head>"``).
    pr_part, _, head_part = raw_key.partition("@")
    if not pr_part.isdigit():
        return None
    if isinstance(raw_val, dict):
        head_sha = raw_val.get("head_sha")
        delivered_at = raw_val.get("delivered_at")
        if not isinstance(head_sha, str) or not isinstance(delivered_at, str):
            return None
        raw_serviced = raw_val.get("serviced_comment_ids")
        serviced = (
            [cid for cid in raw_serviced if isinstance(cid, int)]
            if isinstance(raw_serviced, list)
            else []
        )
        record: StanddownRecord = {
            "head_sha": head_sha,
            "delivered_at": delivered_at,
            "serviced_comment_ids": serviced,
        }
        # Carry the optional expiry deadline (proposal 090) when present
        # AND parseable AND timezone-aware; absent, malformed, or naive →
        # the record simply omits it, matching the graceful degradation for
        # a missing ``serviced_comment_ids``. The guards matter because
        # ``prune_expired_standdowns`` — which now runs on every tend pass —
        # compares ``datetime.fromisoformat(expires)`` against a tz-aware
        # ``now``. An unparseable string raises ``ValueError``; a naive-yet-
        # valid ISO string (hand-edit like ``2026-01-01``) parses cleanly
        # but comparing it against the tz-aware ``now`` raises ``TypeError``.
        # Either would crash every prune on external corruption / hand-edit,
        # so drop a malformed *or* naive deadline the same way a non-string
        # one is dropped — keeping the single serialization boundary
        # corruption-tolerant for format and tz-awareness as well as type.
        # This drop is the corruption-tolerance path for an externally
        # hand-edited ledger, *not* the primary enforcement: tz-awareness is
        # now also enforced where the deadline is established, at the write
        # seam (:func:`deliver_claimed_escalation`) and on the pruner's own
        # ``now``, both via :func:`_require_aware_timestamp`, so a naive value
        # originating in-process fails loudly instead of minting a record this
        # loader would silently strip into an unreleasable standdown.
        raw_expires = raw_val.get("expires_at")
        if isinstance(raw_expires, str):
            try:
                parsed_expires = datetime.fromisoformat(raw_expires)
            except ValueError:
                pass
            else:
                if parsed_expires.tzinfo is not None:
                    record["expires_at"] = raw_expires
        # Carry the optional claim (proposal 135) when present and a string.
        # This is the sole hydration path for both substrates — the JSON
        # adapter's whole-file load and ``state_store.SqliteEscalationLedger.
        # load_escalation_standdowns``' per-row blob rebuild, which constructs a
        # fresh literal and would otherwise drop the key — so the tolerant copy
        # belongs here, in the style of the ``expires_at`` block above. A
        # non-string or absent value leaves the record unclaimed rather than
        # inventing a claim it never made.
        raw_claim = raw_val.get("claim")
        if isinstance(raw_claim, str):
            record["claim"] = raw_claim
        # Carry the optional refuted-release count (proposal 135) when present
        # and a real ``int`` — ``bool`` is an ``int`` subclass, so a hand-edited
        # ``true`` would otherwise load as ``1`` and read as a release that never
        # happened. Absent → the record simply omits it, and a legacy row loads
        # unchanged, exactly as for ``claim`` above. Same shared hydration path,
        # so one edit covers both substrates.
        raw_releases = raw_val.get("refutation_releases")
        if isinstance(raw_releases, int) and not isinstance(raw_releases, bool):
            record["refutation_releases"] = raw_releases
    elif isinstance(raw_val, str):
        # Legacy ``"<pr>@<head>": "<iso-timestamp>"`` entry — no serviced set.
        record = {
            "head_sha": head_part,
            "delivered_at": raw_val,
            "serviced_comment_ids": [],
        }
    else:
        return None
    return int(pr_part), record


def _load_standdowns(path: Path) -> dict[int, StanddownRecord]:
    """Load the ``{pr_number: {head_sha, delivered_at, serviced_comment_ids}}``
    standdown ledger.

    Tolerant of corruption (a non-dict payload yields an empty ledger) and of
    the pre-080 ``"<pr>@<head>": "<iso-timestamp>"`` key form: any such legacy
    entry is migrated to a standdown for its ``pr`` (proposal 080 Phase 3
    upgrade path), with the head recovered from the key suffix — the per-entry
    parse itself lives in :func:`_parse_standdown_entry`, shared with the
    SQLite store's blob hydration (proposal 106). When several keys map to the
    same PR (multiple historical heads), the entry with the latest
    ``delivered_at`` wins, so the informational head is the most recent.
    """
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    standdowns: dict[int, StanddownRecord] = {}
    for raw_key, raw_val in parsed.items():
        entry = _parse_standdown_entry(raw_key, raw_val)
        if entry is None:
            continue
        # Last entry wins when several keys collapse to the same PR (e.g.
        # multiple historical heads on upgrade) — presence is what gates, and
        # the head is informational only.
        pr_number, record = entry
        standdowns[pr_number] = record
    return standdowns


def _write_standdowns(
    path: Path, standdowns: dict[int, StanddownRecord],
) -> None:
    """Serialize the standdown ledger in the ``pr_number``-keyed form."""
    atomic_write(
        path, json.dumps({str(pr): rec for pr, rec in standdowns.items()}),
    )


def load_all_escalation_standdowns(data_dir: Path) -> dict[int, StanddownRecord]:
    """Every persisted standdown from the legacy JSON ledger, canonically keyed.

    The seed source the SQLite backend reads once to copy the legacy
    ``escalation_handoffs.json`` into ``state.db`` (proposal 106), mirroring
    :func:`~maestro.pr_ledger.load_all_pr_ledger_entries`. Delegates to
    :func:`_load_standdowns`, so the pre-080 ``"<pr>@<head>"`` keys are
    collapsed to canonical ``number`` keys at seed time and a missing or
    corrupt file yields ``{}``.
    """
    return _load_standdowns(_standdowns_path(data_dir))


@runtime_checkable
class EscalationLedgerBackend(Protocol):
    """The two persistence primitives the standdown ledger needs (proposal 106).

    A lock-free :meth:`read` for the pure-read functions, and an atomic
    :meth:`mutate` that loads the map, applies the caller's *fn*, persists the
    whole map iff *fn* changed it, and returns *fn*'s result — so a
    load-modify-write stays one atomic unit on either substrate
    (``escalation_handoffs.json`` via :class:`JsonEscalationLedger`, or
    ``state.db`` via :class:`~maestro.state_store.SqliteEscalationLedger`).
    Mirrors :class:`~maestro.pr_ledger.PRLedgerBackend`. The five deliverers
    additionally run their message-store append *inside* one ``mutate`` closure,
    reproducing the historical ledger-lock span that made sibling deliveries
    mutually exclusive (the JSON adapter's
    :func:`~maestro.fileutil.file_lock`; one ``BEGIN IMMEDIATE`` transaction on
    SQLite).
    """

    def read(self) -> dict[int, StanddownRecord]:
        """Return the whole ``{number: record}`` map (``{}`` when absent)."""
        ...

    def mutate(self, fn: Callable[[dict[int, StanddownRecord]], T]) -> T:
        """Load the map, apply *fn*, persist iff changed, return *fn*'s result."""
        ...


class JsonEscalationLedger:
    """Default :class:`EscalationLedgerBackend` — the ``escalation_handoffs.json`` store.

    Lifts the historical module-function bodies verbatim: :meth:`read` is
    :func:`_load_standdowns` and :meth:`mutate` wraps read → *fn* →
    :func:`_write_standdowns` in the same :func:`~maestro.fileutil.file_lock`
    span every locked read-modify-write held, so cross-process atomicity is
    byte-for-byte preserved. The persist-iff-changed guard (a
    :func:`copy.deepcopy` snapshot — the records are plain dicts/lists) is a
    benign strengthening: a no-op closure (e.g. a union of already-serviced
    ids) now writes nothing, matching the pr-ledger's "quiet no-op writes
    nothing" contract. This is the flag-off store (proposal 106); the
    ``state.db`` selection is
    :class:`~maestro.state_store.SqliteEscalationLedger`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = _standdowns_path(data_dir)

    def read(self) -> dict[int, StanddownRecord]:
        """Load the ledger, tolerant of a missing or corrupted file."""
        return _load_standdowns(self._path)

    def mutate(self, fn: Callable[[dict[int, StanddownRecord]], T]) -> T:
        """Load-modify-write under the file lock; persist iff *fn* changed it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self._path):
            standdowns = _load_standdowns(self._path)
            before = copy.deepcopy(standdowns)
            result = fn(standdowns)
            if standdowns != before:
                _write_standdowns(self._path, standdowns)
            return result


def has_standdown(backend: EscalationLedgerBackend, pr_number: int) -> bool:
    """Return whether a durable standdown is recorded for *pr_number*.

    Pure read (no lock) — mirrors the read-only standdown check
    :func:`detect_unlanded_cure_handoff` performs. The Tend route consults this
    to suppress further remediation minting for a PR already handed up. Reads
    through the injected :class:`EscalationLedgerBackend` (proposal 106).
    """
    return pr_number in backend.read()


def active_standdown_prs(backend: EscalationLedgerBackend) -> set[int]:
    """Return the set of numbers carrying a durable standdown.

    Read-only (no lock) sibling of :func:`has_standdown` that returns the whole
    set in one read — for the known-state PR-LAPS lap summary and the
    ``maestro pr-laps`` diagnostic, which surface numbers whose remediation
    minting is suppressed. Claimed-escalation records (proposal 090 — the only
    standdowns carrying ``expires_at``) are **included**: rule 12
    (:func:`~maestro.audit_checks_phantom.check_phantom_escalation`) keys overwhelmingly
    on real PR numbers (its ``Remediate PR #N`` remediation laps), so a stood-down
    PR must still be annotated ``[stood-down]`` in the diagnostic operators use to
    reason about remediation pileups — hiding a genuinely-suppressed PR is the
    worse failure. ``expires_at`` marks a *claimed-escalation* record, not an
    *issue-keyed* one, so a rare genuinely-plain-issue key may render as a
    ``#<n> [stood-down]`` entry; that display-only mislabel is accepted because
    the number is genuinely stood down and the alternative drops real PRs. The
    per-number minting gate uses :func:`has_standdown`, unaffected either way.
    Empty set when no ledger exists or none are recorded. Reads through the
    injected :class:`EscalationLedgerBackend` (proposal 106).
    """
    return set(backend.read())


def standdown_claims(records: Mapping[int, StanddownRecord]) -> dict[int, str]:
    """Return ``{number: claim}`` for every record carrying a ``claim`` (135).

    A **pure** projection over an already-read ledger mapping — the caller does
    the single :meth:`EscalationLedgerBackend.read` and folds it into every view
    it needs (presence via :func:`active_standdown_prs`-style ``set(records)``,
    the claims here, the delivery times via
    :func:`standdown_delivery_times`), so one PR-LAPS line costs one read rather
    than one per view and no two views can skew against each other mid-iteration.

    It surfaces *what each active standdown asserts* rather than merely that one
    exists — the input the validity oracle
    (:func:`~maestro.standdown_validity.measure_contradicted_standdowns`) folds
    against a live owed-act reading to name a refuted latch.

    Records are returned as **raw claim strings**, deliberately un-parsed, so
    this module needs no import of :mod:`maestro.standdown_validity` — the
    oracle is a leaf that imports the owed-act composer, and the reverse import
    would couple the ledger to it. A legacy record written before the ``claim``
    key existed is simply absent from the map (unclaimed), never defaulted to a
    claim it never made.
    """
    return {
        number: record["claim"]
        for number, record in records.items()
        if "claim" in record
    }


def standdown_delivery_times(
    records: Mapping[int, StanddownRecord],
) -> dict[int, str]:
    """Return ``{number: delivered_at}`` for every record carrying a timestamp.

    The sibling projection of :func:`standdown_claims` over the same single
    read. The validity oracle needs it as a *staleness floor*: a readiness
    snapshot observed before a claim was latched cannot refute that claim, and
    snapshots stop being stamped once the latch suppresses the remediation that
    would write them, so an unbounded reading would be stale by construction.

    A record with a missing or non-string ``delivered_at`` (the parser only ever
    writes a string, but a hand-edited ledger may not) is simply absent, which
    the reader treats as *unfiltered* rather than inventing a floor. Pure.
    """
    return {
        number: record["delivered_at"]
        for number, record in records.items()
        if isinstance(record.get("delivered_at"), str) and record["delivered_at"]
    }


def clear_standdown(
    backend: EscalationLedgerBackend,
    pr_number: int,
    *,
    expected_delivered_at: str | None = None,
) -> bool:
    """Remove any standdown record for *pr_number*; return whether one existed.

    Clears both the ``pr_number``-keyed form and any migrated legacy
    ``"<pr>@<head>"`` entries for the PR (the backend's whole-map replace drops
    the legacy keys once :func:`_load_standdowns` has collapsed them to one
    ``pr`` key). Runs its read-modify-write inside one atomic
    :meth:`EscalationLedgerBackend.mutate` closure (the JSON adapter's
    :func:`~maestro.fileutil.file_lock` span, or one ``BEGIN IMMEDIATE`` on
    SQLite) so a concurrent deliver/prune cannot interleave a lost update. A
    no-op (returns ``False``, and — the map unchanged — writes nothing) when the
    PR has no standdown.

    *expected_delivered_at* narrows the clear to **one authored row**: given, the
    record is deleted only while its ``delivered_at`` still matches, so a caller
    releasing a suppression *it* delivered cannot delete a foreign deliverer's
    row recorded under the same PR key after the caller's own was cleared by a
    non-owner (the #3630 green-handoff arm's CI-RED release). The match is read
    inside the same ``mutate`` closure as the delete, so no read-then-clear
    window exists. ``None`` (the default) keeps the unconditional
    clear-by-PR-number every other caller uses.
    """
    def _apply(standdowns: dict[int, StanddownRecord]) -> bool:
        record = standdowns.get(pr_number)
        if record is None:
            return False
        if (
            expected_delivered_at is not None
            and record["delivered_at"] != expected_delivered_at
        ):
            return False
        del standdowns[pr_number]
        return True

    return backend.mutate(_apply)


def unserviced_comment_ids(
    backend: EscalationLedgerBackend, pr_number: int, current_ids: set[int],
) -> set[int]:
    """Return the subset of *current_ids* not yet serviced under the standdown.

    Pure read (no lock) — mirrors :func:`has_standdown`. *current_ids* are the
    genuinely-unanswered review-thread comment ids detected for *pr_number* on
    this tend pass; the result is those minus the standdown's recorded serviced
    set — i.e. the threads that have never earned a remediation lap under this
    standdown (#2798). Returns an empty set when no standdown is recorded for
    *pr_number*: the carve-out applies only while a standdown is in force. Reads
    through the injected :class:`EscalationLedgerBackend` (proposal 106).
    """
    record = backend.read().get(pr_number)
    if record is None:
        return set()
    return current_ids - set(record["serviced_comment_ids"])


def mark_comment_ids_serviced(
    backend: EscalationLedgerBackend, pr_number: int, ids: set[int],
) -> None:
    """Union *ids* into the standdown's serviced set; no-op without a standdown.

    Runs the read-modify-write inside one atomic
    :meth:`EscalationLedgerBackend.mutate` closure (like :func:`clear_standdown`)
    so a concurrent deliver/prune cannot interleave a lost update. When no
    standdown is recorded for *pr_number* nothing is written — a serviced set is
    meaningful only alongside a live standdown, and the whole record (serviced
    set included) is dropped on merge/close by :func:`prune_resolved_standdowns`
    / :func:`clear_standdown`. The serviced set is stored sorted for a stable
    on-disk shape (#2798); when *ids* are all already serviced the map is
    unchanged and the backend writes nothing (the persist-iff-changed contract,
    proposal 106).
    """
    def _apply(standdowns: dict[int, StanddownRecord]) -> None:
        record = standdowns.get(pr_number)
        if record is None:
            return
        record["serviced_comment_ids"] = sorted(
            set(record["serviced_comment_ids"]) | ids,
        )

    backend.mutate(_apply)


def reconcile_serviced_comment_ids(
    backend: EscalationLedgerBackend,
    pr_number: int,
    still_unanswered_ids: set[int],
) -> set[int]:
    """Drop *still_unanswered_ids* from *pr_number*'s serviced set; return dropped.

    Repairs a serviced set corrupted by the pre-091 admission-time stamp, which
    marked a thread serviced the instant a lap was queued — before its terminal
    reply ever posted, so a reply that never delivered stayed muted forever.
    *still_unanswered_ids* are the comment ids GitHub still shows unanswered on
    this tend pass (each lacks a terminal Maestro reply by construction), so any
    of them present in the standdown's serviced set was *falsely* stamped:
    dropping it re-enters it into the recomputed ``unserviced`` set, reopening
    the #2798 carve-out valve so the loop answers it. Returns the ids actually
    dropped (``set()`` when none intersected, writing nothing). Runs the
    read-modify-write under the ledger :func:`~maestro.fileutil.file_lock` (like
    :func:`mark_comment_ids_serviced` / :func:`clear_standdown`); a no-op
    returning an empty set when the PR has no standdown — a serviced set is
    meaningful only alongside a live standdown. A genuinely-answered thread
    carries a terminal marker, is excluded from *still_unanswered_ids*, and is
    never touched, so forward-stamping stays honest while legacy sets self-heal.
    An empty *still_unanswered_ids* short-circuits to an empty set before any
    backend call — nothing can be dropped, so the common
    stood-down-PR-with-no-eligible-thread pass pays no ``mutate`` (proposal 106).
    """
    if not still_unanswered_ids:
        return set()

    def _apply(standdowns: dict[int, StanddownRecord]) -> set[int]:
        record = standdowns.get(pr_number)
        if record is None:
            return set()
        serviced = set(record["serviced_comment_ids"])
        dropped = serviced & still_unanswered_ids
        if not dropped:
            return set()
        record["serviced_comment_ids"] = sorted(serviced - dropped)
        return dropped

    return backend.mutate(_apply)


def prune_resolved_standdowns(
    backend: EscalationLedgerBackend, is_pr_resolved: Callable[[int], bool],
) -> list[int]:
    """Clear standdowns for PRs the host API reports merged/closed.

    Consults *is_pr_resolved* — a predicate returning ``True`` when the PR is
    merged **or** closed per the host API — for each PR with a recorded
    standdown (a small set), removing the resolved ones so a genuinely new
    problem on a reopened PR can earn a fresh knock. Returns the sorted list of
    cleared PR numbers (``[]`` when nothing was recorded or nothing resolved).
    The empty-ledger fast path is a lock-free :meth:`EscalationLedgerBackend.read`
    (replacing the JSON path's ``path.exists()`` check, proposal 106); otherwise
    the read-check-write runs inside one atomic ``mutate`` closure.

    The predicate is deliberately *not* wrapped in a swallowing handler: a
    raising *is_pr_resolved* propagates loudly rather than silently leaving a
    stale standdown in place (fail-loud). Callers pass a fail-closed predicate
    built from the host oracle.
    """
    if not backend.read():
        return []

    def _apply(standdowns: dict[int, StanddownRecord]) -> list[int]:
        resolved = [pr for pr in standdowns if is_pr_resolved(pr)]
        if not resolved:
            return []
        for pr in resolved:
            del standdowns[pr]
        return sorted(resolved)

    return backend.mutate(_apply)


def prune_expired_standdowns(
    backend: EscalationLedgerBackend, *, now: str,
) -> list[int]:
    """Clear standdowns whose ``expires_at`` deadline has passed.

    Proposal 090's age-based complement to :func:`prune_resolved_standdowns`.
    Removes every record carrying an ``expires_at`` that is ``<= now`` (both
    parsed as ISO timestamps), returning the sorted list of cleared keys
    (``[]`` when nothing was recorded or nothing expired). Records without an
    ``expires_at`` — every PR/reality-refutation handoff — are never age-expired
    and keep their merge/close-only release via :func:`prune_resolved_standdowns`.

    Issue-keyed claimed-escalation records (:func:`deliver_claimed_escalation`)
    carry ``expires_at`` because :func:`prune_resolved_standdowns` can never
    release a plain issue number (it never "merges as a PR"); ageing them out
    keeps the ledger bounded. The empty-ledger fast path is a lock-free
    :meth:`EscalationLedgerBackend.read` (replacing the JSON path's
    ``path.exists()`` check, proposal 106); otherwise the read-check-write runs
    inside one atomic ``mutate`` closure. *now* is the current UTC ISO
    timestamp, and must be timezone-aware: a malformed or naive value raises a
    ``ValueError`` naming the parameter (:func:`_require_aware_timestamp`) at
    the top of the call, rather than a ``TypeError`` from the aware/naive
    comparison inside ``_apply``. The empty-ledger fast path runs ahead of that
    check, so the zero-cost no-op is unaffected.
    """
    if not backend.read():
        return []
    now_dt = _require_aware_timestamp(now, "now")

    def _apply(standdowns: dict[int, StanddownRecord]) -> list[int]:
        expired = [
            key
            for key, rec in standdowns.items()
            if (expires := rec.get("expires_at")) is not None
            and datetime.fromisoformat(expires) <= now_dt
        ]
        if not expired:
            return []
        for key in expired:
            del standdowns[key]
        return sorted(expired)

    return backend.mutate(_apply)


class RefutedReleaseOutcome(NamedTuple):
    """What one :func:`prune_refuted_standdowns` pass did (proposal 135).

    ``released`` is the sorted list of PR keys whose latch was dropped this pass
    — each one having durably appended its own withdrawal message first.
    ``capped`` is the sorted list of PR keys that read ``REFUTED`` but had
    already spent their whole release budget, *and* for which this pass wrote the
    one terminal notice; a capped PR keeps its record, and a later pass over the
    same PR re-reads the terminal thread, writes nothing, and reports nothing —
    so ``capped`` names a transition, never a standing state.
    """

    released: list[int]
    capped: list[int]


def prune_refuted_standdowns(
    backend: EscalationLedgerBackend,
    live_owed_act: Callable[[int], OwedAct | None],
    *,
    data_dir: Path | None = None,
    blockers: Mapping[int, str] | None = None,
    release_cap: int = 0,
    message_store: MessageStore | None = None,
    now: str | None = None,
) -> RefutedReleaseOutcome:
    """Clear standdowns whose latched *claim* the live owed-act reading refutes.

    Proposal 135's third release path, sibling of
    :func:`prune_resolved_standdowns` (the host says merged/closed) and
    :func:`prune_expired_standdowns` (a deadline passed). Those two release on
    facts *about the record*; this one re-asks the question the record answered.
    A :attr:`~maestro.standdown_validity.StanddownClaim.HANDOFF_READY` record
    asserts "no owned code work remains, only Thomas can act" — a claim a later
    reading routinely refutes (a sibling merge turns the handed-off PR
    ``CONFLICTING``, so the owed act flips back to *write code*) while the latch
    goes on suppressing the remediation that would clear it.

    For each recorded key it parses the persisted ``claim`` through
    :func:`~maestro.standdown_validity.parse_standdown_claim`, reads the PR's
    current ``OwedAct | None`` from *live_owed_act*, and acts on the record when
    :func:`~maestro.standdown_validity.assess_standdown_validity` returns
    :attr:`~maestro.standdown_validity.StanddownValidity.REFUTED`.

    **The release is spoken, and it is budgeted.** With *release_cap* ≥ 1 each
    refuted record first appends one id-keyed withdrawal message naming the PR,
    the *blockers* entry that refuted the claim, and the owed act — because the
    handoff told Thomas that only he could land this PR, and silently deleting
    the ledger row would leave that now-measured-false claim standing unretracted
    in his inbox. Once :func:`count_refutation_releases` shows the PR has already
    spent *release_cap* releases the latch becomes **permanent**: the record is
    left in place, one terminal notice is appended if it is not already on record,
    and nothing further is written on any later pass. Unbounded, a PR flapping
    between merge-ready and ``CONFLICTING`` would cycle handoff → refutation →
    handoff forever, trading a silent deadlock for a message storm.

    *release_cap* ``0`` (the default) is the unbudgeted legacy shape: refuted
    records are released exactly as before and no message is written. The wired
    caller (``PrRemediationController.route_feedback``) always passes the
    configured ``[loop] standdown_refutation_release_cap`` (≥ 1).

    **Delivery-truth bind.** Mirroring :func:`deliver_unwinnable_race`, a message
    is never trusted from ``save_message``'s return: the thread is re-read after
    the write, and a record whose withdrawal did **not** land is *not* released —
    a silent retraction is exactly the failure this step exists to prevent, so
    the latch stays and the next pass retries.

    Selection keys on the **claim**, never on the absence of ``expires_at``: an
    ``expires_at``-bearing claimed-escalation record is routinely keyed on a real
    PR number, and its
    :attr:`~maestro.standdown_validity.StanddownClaim.ESCALATION_CLAIMED` claim
    asserts something the owed-act oracle does not answer — so the oracle reads it
    ``HOLDS`` and it survives here even when that PR reads ``CODE_OWED``. An
    unclaimed legacy row survives for the same reason. The claim is the fact
    :func:`deliver_handoff` recorded for exactly this purpose.

    The empty-ledger fast path is a lock-free
    :meth:`EscalationLedgerBackend.read` (both siblings' shape); otherwise the
    read-check-write runs inside one atomic ``mutate`` closure. A raising
    *live_owed_act* propagates rather than silently leaving a refuted latch in
    place, matching :func:`prune_resolved_standdowns`'s fail-loud predicate
    contract — callers pass a reading composed from facts already measured, so
    the release costs no host, merge-oracle or SDK call.

    *data_dir* roots the :class:`~maestro.messaging.MessageStore` when one is not
    injected via *message_store* (tests inject); it is required only when
    *release_cap* ≥ 1. *blockers* maps PR number → the measured merge-block value
    the tend walk derived (``TendPRsResult.owned_blockers_observed``); a missing
    entry degrades the message to generic prose rather than failing. *now*
    injects the ISO timestamp.
    """
    if not backend.read():
        return RefutedReleaseOutcome([], [])
    store: MessageStore | None = None
    if release_cap > 0:
        if message_store is not None:
            store = message_store
        elif data_dir is not None:
            store = MessageStore(data_dir)
        else:
            msg = "data_dir or message_store is required when release_cap >= 1"
            raise ValueError(msg)
    reasons = blockers if blockers is not None else {}

    def _withdraw(
        store: MessageStore, pr_number: int, index: int, owed_act: str,
    ) -> bool:
        """Append the *index*-th withdrawal; ``True`` iff it durably landed.

        *owed_act* is the reading the selection pass already took — never a
        second :func:`live_owed_act` call. The predicate is documented as
        arbitrary and fail-loud, so a re-invocation could return a *different*
        act than the one that produced the ``REFUTED`` verdict, and the
        retraction Thomas reads would then name an act that did not motivate it.
        """
        message_id = _refutation_withdrawal_message_id(pr_number, index)
        store.save_message(Message(
            message_id=message_id,
            timestamp=now if now is not None else datetime.now(UTC).isoformat(),
            sender="maestro",
            content=_refutation_withdrawal_message(
                pr_number, reasons.get(pr_number, ""), owed_act,
            ),
            message_type="question",
        ))
        if store.get_thread(message_id):
            return True
        logger.warning(
            "escalation-bridge: withdrawal message for refuted stand-down on "
            "PR #%d did not land on post-write re-read; keeping the latch "
            "rather than releasing it silently",
            pr_number,
        )
        return False

    def _cap(store: MessageStore, pr_number: int) -> bool:
        """Append the terminal notice once; ``True`` iff written this pass."""
        message_id = _refutation_capped_message_id(pr_number)
        if store.get_thread(message_id):
            # Already terminal — repeated passes over a capped PR write nothing.
            return False
        store.save_message(Message(
            message_id=message_id,
            timestamp=now if now is not None else datetime.now(UTC).isoformat(),
            sender="maestro",
            content=_refutation_capped_message(pr_number, release_cap),
            message_type="question",
        ))
        return bool(store.get_thread(message_id))

    def _apply(standdowns: dict[int, StanddownRecord]) -> RefutedReleaseOutcome:
        # One ``live_owed_act`` call per key, and the act that produced the
        # verdict is carried forward to the withdrawal prose: the predicate is
        # documented as arbitrary, so re-reading it per message could name an
        # act that did not motivate the release.
        refuted: list[tuple[int, OwedAct | None]] = []
        for key in sorted(standdowns):
            act = live_owed_act(key)
            if assess_standdown_validity(
                parse_standdown_claim(standdowns[key].get("claim")), act,
            ) is StanddownValidity.REFUTED:
                refuted.append((key, act))
        released: list[int] = []
        capped: list[int] = []
        for key, act in refuted:
            if store is None:
                # Unbudgeted legacy shape (``release_cap <= 0``): release exactly
                # as before, speaking neither a withdrawal nor a terminal notice.
                del standdowns[key]
                released.append(key)
                continue
            prior = count_refutation_releases(store, key, cap=release_cap)
            if prior >= release_cap:
                if _cap(store, key):
                    capped.append(key)
                continue
            if _withdraw(
                store, key, prior + 1, act.value if act is not None else "",
            ):
                del standdowns[key]
                released.append(key)
        return RefutedReleaseOutcome(released, capped)

    return backend.mutate(_apply)


def _message_id(pr_number: int, head_sha: str) -> str:
    """Return the base message id for a *pr_number* handoff.

    Shaped ``handoff-<pr>-<head_tag>`` so the handoff-relapse sensor
    (:func:`maestro.remediation_tracker.measure_handoff_relapse`) can recover
    the PR number from the id. The head tag is informational only; the ledger
    is keyed on ``pr_number``. A repeat handoff after a released stand-down
    takes an ordinal suffix via :func:`_fresh_handoff_message_id`.
    """
    head_tag = head_sha[:8] if head_sha else "nohead"
    return f"handoff-{pr_number}-{head_tag}"


# Upper bound on the ordinal probe in :func:`_fresh_handoff_message_id`.
_HANDOFF_ID_PROBE_CAP = 64


def _fresh_handoff_message_id(
    store: MessageStore, pr_number: int, head_sha: str,
) -> str:
    """Return a handoff id no earlier delivery for *pr_number* already holds.

    A PR can be handed off again once its stand-down is released (a refuted
    release, or the CI-pending arm's RED release); reusing the base id would
    merge both deliveries into one :meth:`MessageStore.get_thread` and stop
    ``mark_read`` at the first match (#4390). The first delivery keeps the bare
    :func:`_message_id`; repeats append ``-2``, ``-3``, … — still matching
    ``^handoff-<pr>-`` — probing at most :data:`_HANDOFF_ID_PROBE_CAP` ordinals.
    Past the cap the overflow id is returned unprobed (so a further repeat
    shares it) and a warning names the exhausted probe.
    """
    base = _message_id(pr_number, head_sha)
    if not store.get_thread(base):
        return base
    for ordinal in range(2, _HANDOFF_ID_PROBE_CAP + 1):
        candidate = f"{base}-{ordinal}"
        if not store.get_thread(candidate):
            return candidate
    overflow = f"{base}-{_HANDOFF_ID_PROBE_CAP + 1}"
    logger.warning(
        "escalation-bridge: handoff id probe for PR #%d exhausted %d ordinals; "
        "reusing %s",
        pr_number, _HANDOFF_ID_PROBE_CAP, overflow,
    )
    return overflow


def _latest_pr_head_sha(data_dir: Path, pr_number: int, n_cycles: int) -> str:
    """Return the most recent recorded ``pr_head_sha`` for *pr_number*.

    Scans the trailing *n_cycles* ``task_history.jsonl`` records in append
    order for ``Remediate PR #<n>`` tasks matching *pr_number*, returning the
    last non-empty ``pr_head_sha`` seen. Returns ``""`` when no matching record
    carries a head SHA (legacy records, or none recorded one).
    """
    sha = ""
    for task in _read_recent_task_tail(data_dir, n_cycles):
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        if pr_number_from_remediation_goal(goal) != pr_number:
            continue
        head = task.get("pr_head_sha")
        if isinstance(head, str) and head:
            sha = head
    return sha


def detect_unlanded_cure_handoff(
    data_dir: Path,
    *,
    backend: EscalationLedgerBackend,
    threshold: int = HANDOFF_STAMP_THRESHOLD,
    n_cycles: int = HANDOFF_SCAN_CYCLES,
    is_pr_merged: Callable[[int], bool] | None = None,
    is_pr_settled: Callable[[int], bool] | None = None,
    no_progress_laps: dict[int, int] | None = None,
) -> UnlandedCureHandoff | None:
    """Return the oldest unhanded-off unlanded-cure handoff, or ``None``.

    By default consumes
    :func:`~maestro.remediation_tracker.measure_unlanded_remediations` — the
    git-merge-truth sensor — to find PRs stamped success/partial yet not merged
    into ``main`` within the trailing *n_cycles* window (**gross** laps). Among
    those whose cumulative lap count is ``>= threshold`` (the lap budget) and for
    which **no ``pr_number``-keyed standdown is recorded**, returns the
    lowest-numbered (oldest, per oldest-bug-first doctrine). Returns ``None``
    when none qualify or all qualifying ones already have a standdown.

    When *no_progress_laps* is supplied (proposal 089 step 5: the
    progress-aware lap budget), it **replaces** the gross-lap sensor as the lap
    counts — a ``{pr_number: consecutive_no_progress_laps}`` mapping from
    :func:`~maestro.remediation_tracker.measure_no_progress_laps`. A PR that
    clears a freshly-arrived blocker every lap (the healthy churn of a fast
    main) never crosses the budget, while one grinding on identical observations
    still does. Because that sensor does not itself consult git-merge-truth, a
    candidate is also skipped when *is_pr_merged* reports it merged — so a merged
    PR is never handed off for merge-or-close.

    Unlike the pre-080 detector, a fresh head SHA on a still-unmerged PR does
    **not** resurface a handoff: the standdown is keyed on ``pr_number`` alone,
    so once a PR is handed up it stays silent across head changes until
    :func:`prune_resolved_standdowns` clears it on a merged/closed verdict.

    *is_pr_merged* is forwarded to the gross-lap sensor (and used as the merged
    guard in progress-aware mode); leaving it ``None`` uses that function's
    default git-merge-truth oracle.

    *is_pr_settled* is the symmetric **settled** guard (proposal-less bug fix
    for #2967): a candidate is skipped when it reports ``True`` — i.e. the PR is
    merged **or** closed-unmerged — in *both* the gross-lap and progress-aware
    branches, so it subsumes the merged-only *is_pr_merged* guard. This aligns
    detection with :func:`prune_resolved_standdowns`, whose release predicate is
    the same merged-or-closed ``is_pr_settled`` chokepoint: a PR Thomas closes in
    response to the handoff is released *and* never re-handed-off, closing the
    every-iteration re-knock where the merged-only guard let a closed PR resurface
    a fresh merge-or-close message. Leaving it ``None`` preserves the pre-fix
    merged-only behaviour (used by the tests that inject only *is_pr_merged*).

    *backend* is the injected
    :class:`EscalationLedgerBackend` the standdown-presence check reads through
    (proposal 106); the lap sensors and head-SHA scan stay path-based on
    *data_dir*. Pure read, zero SDK cost.
    """
    if threshold <= 0:
        return None
    if no_progress_laps is not None:
        laps = no_progress_laps
        merged_guard = is_pr_merged
    else:
        laps = measure_unlanded_remediations(
            data_dir, n_cycles, is_pr_merged=is_pr_merged,
        )
        merged_guard = None
    if not laps:
        return None
    standdowns = backend.read()
    for pr_number in sorted(laps):
        stamp_count = laps[pr_number]
        if stamp_count < threshold:
            continue
        if pr_number in standdowns:
            continue
        # Symmetric settled guard (#2967): skip a merged OR closed-unmerged PR,
        # mirroring prune_resolved_standdowns' merged-or-closed release. Applies
        # in both the gross-lap and progress-aware branches and subsumes the
        # merged-only merged_guard below.
        if is_pr_settled is not None and is_pr_settled(pr_number):
            continue
        if merged_guard is not None and merged_guard(pr_number):
            continue
        head_sha = _latest_pr_head_sha(data_dir, pr_number, n_cycles)
        return UnlandedCureHandoff(
            pr_number=pr_number, head_sha=head_sha, stamp_count=stamp_count,
        )
    return None


# Fixed character budget for the capability line :func:`deliver_handoff`
# prepends to a handoff message: the owning plan's first acceptance criterion —
# or its title when criteria are absent — is truncated to this width so a long
# criterion cannot bloat the merge-or-close message. The generic body is left
# unchanged, so a handoff that resolves no plan is byte-identical to the
# pre-enrichment text.
_HANDOFF_CAPABILITY_CAP = 200


def _capability_line(plan: PlanCapability) -> str | None:
    """Return the bounded "what the merge lands" line for *plan*, or ``None``.

    The single owner of the capability-line logic: every :func:`deliver_handoff`
    caller passes a ``plan_lookup`` and never composes the line itself, so the
    extraction, fallback, and truncation live only here. Prefers the plan's first
    non-blank ``acceptance_criteria`` entry — the testable capability the merge
    would land — and falls back to the plan title when the criteria are absent or
    blank. The chosen text is truncated to :data:`_HANDOFF_CAPABILITY_CAP`
    characters (ellipsised) so a long criterion cannot bloat the message. Returns
    ``None`` when neither source yields non-blank text, so the caller omits the
    line cleanly rather than emitting an empty or dangling one.
    """
    source = ""
    for candidate in plan.acceptance_criteria:
        if isinstance(candidate, str) and candidate.strip():
            source = candidate.strip()
            break
    if not source and isinstance(plan.title, str):
        source = plan.title.strip()
    if not source:
        return None
    if len(source) > _HANDOFF_CAPABILITY_CAP:
        source = source[: _HANDOFF_CAPABILITY_CAP - 1].rstrip() + "…"
    return f"This merge would land: {source}"


def _handoff_message(
    handoff: UnlandedCureHandoff, plan: PlanCapability | None = None,
) -> str:
    """Render the merge-or-close message addressed to Thomas.

    ``stamp_count`` is described as unlanded remediation laps rather than
    success/partial stamps: in progress-aware mode (proposal 094) it is the
    consecutive *no-progress* lap count, not the success/partial stamp count, so
    a "stamped success/partial Nx" phrasing would undercount the actual stamps.
    "N unlanded remediation lap(s)" is accurate in both the gross-count and
    progress-aware modes.

    When *plan* is the PR's owning plan, one bounded capability line
    (:func:`_capability_line`) naming what the merge would land is prepended, so
    the ping states the accomplishment behind the ask rather than status alone.
    When *plan* is ``None`` — or resolves to no nameable capability — the emitted
    message is byte-identical to the generic merge-or-close text below (no empty
    or dangling capability line).
    """
    body = (
        f"Handoff — PR #{handoff.pr_number} carries an implemented cure that has "
        f"not landed after {handoff.stamp_count} unlanded remediation lap(s), and "
        f"git-merge-truth shows it is not merged into main. Only you can land it: "
        f"please merge PR #{handoff.pr_number} or close/retire it. I am standing "
        f"down — no further remediation laps will be minted for this PR until it "
        f"merges or closes."
    )
    if plan is not None:
        line = _capability_line(plan)
        if line is not None:
            return f"{line}\n{body}"
    return body


def _ready_handoff_message(
    handoff: UnlandedCureHandoff, plan: PlanCapability | None = None,
) -> str:
    """Render the merge-or-close message for a ``HANDOFF_READY`` premise.

    :func:`_handoff_message` states lap exhaustion, which a readiness handoff
    (audit rule 22, the mergeable-flip merge request) never measured — rule 22
    passes ``stamp_count=1`` for a PR that may have had no lap at all (#4390).
    This body states the premise those callers acted on: the PR is merge-ready.
    The capability line is prepended exactly as :func:`_handoff_message` does.
    """
    body = (
        f"Handoff — PR #{handoff.pr_number} is merge-ready and waiting on you, "
        f"and git-merge-truth shows it is not merged into main. Only you can "
        f"land it: please merge PR #{handoff.pr_number} or close/retire it. I "
        f"am standing down — no further remediation laps will be minted for "
        f"this PR until it merges or closes."
    )
    line = _capability_line(plan) if plan is not None else None
    return body if line is None else f"{line}\n{body}"


def ci_pending_green_handoff_message(pr_number: int) -> str:
    """Render the merge-or-close message for the CI-pending green-handoff arm.

    :func:`_handoff_message` states the lap-exhaustion premise — "has not landed
    after N unlanded remediation lap(s)" — which is *false* for the #3630 arm
    (``[loop] ci_pending_green_handoff_enabled``): it fires at the CI-pending
    hold's fire point, where the premise is instead "my demanding local gates
    (ruff → mypy) pass on this head while CI has not concluded yet". Asking
    Thomas to merge without naming that pending CI would overclaim, so the arm
    renders its own premise here rather than reusing the lap-exhaustion text.

    The stand-down this message announces is the same ``pr_number``-keyed one
    every handoff records, with one arm-specific release: the gate clears it if
    CI later concludes RED, so the loop can still mint a lap to fix its own red
    (the local pair runs ruff and mypy only — no pytest, no coverage, no
    browser — so local green is genuinely weaker than CI green).
    """
    return (
        f"Handoff — PR #{pr_number} passes my demanding local gates (ruff → "
        f"mypy) on its own head, but its CI has not concluded yet and "
        f"git-merge-truth shows it is not merged into main. Only you can land "
        f"it: please merge PR #{pr_number} once CI goes green, or close/retire "
        f"it. I am standing down — no further remediation laps will be minted "
        f"for this PR until it merges or closes, except to fix a CI failure if "
        f"CI concludes red."
    )


def _reality_refutation_message(pr_number: int, refute_count: int) -> str:
    """Render the reality-refutation merge-or-close message addressed to Thomas."""
    return (
        f"Reality-refutation — I have asserted PR #{pr_number} resolved across "
        f"{refute_count} consecutive audit cycles, but git-merge-truth shows it "
        f"is still not merged into main. My narration cannot overrule git: only "
        f"you can land it. Please merge PR #{pr_number} or close/retire it. I am "
        f"standing down — no further remediation laps will be minted for this PR "
        f"until it merges or closes."
    )


def _claimed_escalation_message_id(issue_number: int, delivered_at: str) -> str:
    """Return the message id for an *issue_number* claimed-escalation knock.

    Folds the delivery timestamp in as an epoch tag —
    ``claimed-escalation-<issue>-<delivered_at>`` — so a re-escalation delivered
    after :func:`prune_expired_standdowns` has aged the 30-day record out earns a
    *distinct* id rather than colliding with the prior row. A collision would
    break :meth:`~maestro.messaging.MessageStore.mark_read` (which stops at the
    first id match) and conflate :meth:`~maestro.messaging.MessageStore.get_thread`
    (which returns every row sharing the id). The unmodified ``#{issue_number}``
    still names the issue in the message *content* — the handle rule 12
    self-extinguishes on — so uniquifying the id does not weaken it. This mirrors
    how :func:`_message_id` varies a PR handoff's id by ``head_sha``; a plain
    issue has no head, so the delivery timestamp is its natural epoch.
    """
    return f"claimed-escalation-{issue_number}-{delivered_at}"


def _claimed_escalation_message(issue_number: int, lap_count: int) -> str:
    """Render the claimed-escalation merge-or-close message addressed to Thomas.

    Names the issue as ``#{issue_number}`` so
    :func:`~maestro.audit_checks.extract_issue_numbers` recovers it and rule 12
    self-extinguishes the next cycle — the escalation verb becomes a delivered
    knock rather than prose. *lap_count* is a lower bound: the caller passes the
    finding's cite count, which :data:`~maestro.audit_checks.MAX_CITES_PER_FINDING`
    clips at 5, so the prose says "at least ``lap_count``" rather than overstating
    a precise figure the finding itself may have counted higher.

    Unlike the sibling handoff/reality-refutation knocks — whose standdowns are
    non-expiring and lift only on merge/close — this arm's standdown carries a
    :data:`_CLAIMED_ESCALATION_EXPIRY` deadline and is also cleared by
    :func:`prune_expired_standdowns`. So the suppression promise names that
    time bound (derived from the constant, never a hard-coded figure that could
    drift) alongside merge/close, rather than claiming an open-ended hold the
    ledger does not actually honour for a PR left open past the deadline.
    """
    return (
        f"Claimed-escalation — I stamped 'escalating' for #{issue_number} across "
        f"at least {lap_count} partial remediation lap(s) but never sent the "
        f"message that escalation promises. Sending it now: only you can land it. "
        f"Please merge #{issue_number} or close/retire it. I am standing down — no "
        f"further remediation laps will be minted for it for "
        f"~{_CLAIMED_ESCALATION_EXPIRY.days} days or until it merges or closes."
    )


def _retirement_disposition_message_id(pr_number: int) -> str:
    """Return the disposition message id for a *pr_number* retirement handoff.

    Shaped ``disposition-<pr>`` — deliberately distinct from
    :func:`_message_id`'s ``handoff-<pr>-<head_tag>`` so
    :func:`~maestro.remediation_tracker.measure_handoff_relapse`'s
    ``^handoff-(\\d+)-`` id pattern never miscounts a retirement disposition as
    an unlanded-cure handoff relapse. A retirement is keyed on ``pr_number``
    alone — the plan is being let go, not re-lapped — so no head tag is folded in.
    """
    return f"disposition-{pr_number}"


def _retirement_pr_comment(pr_number: int, plan_title: str) -> str:
    """Render the close-or-keep comment posted on the PR at retirement."""
    return (
        f'I am retiring my evolution plan for this PR — "{plan_title}". It has '
        f"stayed open past the orphaned-cure retirement ceiling with a "
        f"tend-confirmed merge conflict, so I have dropped it from my active plan "
        f"set. Please **close PR #{pr_number}** to let it go, or **resolve the "
        f"conflict and merge** to keep the work. I have handed this decision to "
        f"Thomas and will not mint further remediation laps for this PR until it "
        f"merges or closes."
    )


def _retirement_thomas_message(
    pr_number: int, plan_title: str, pr_url: str,
) -> str:
    """Render the durable close-or-keep message addressed to Thomas."""
    return (
        f'Retirement — I am retiring my evolution plan "{plan_title}" for PR '
        f"#{pr_number} ({pr_url}): implemented-but-unmerged past the "
        f"orphaned-cure ceiling with a tend-confirmed conflict. Only you can "
        f"resolve it: please close PR #{pr_number} to let the plan go, or resolve "
        f"the conflict and merge it to keep the work. I have dropped the plan "
        f"from my active set and am standing down — no further remediation laps "
        f"will be minted for this PR until it merges or closes."
    )


def deliver_reality_refutation(
    data_dir: Path,
    pr_number: int,
    refute_count: int,
    *,
    backend: EscalationLedgerBackend,
    message_store: MessageStore | None = None,
    now: str | None = None,
) -> bool:
    """Deliver one reality-refutation escalation for *pr_number*; return whether it did.

    Proposal 088 Phase 2's actuator — the success-domain twin of
    :func:`deliver_handoff`. When a git-grounded audit finding for *pr_number*
    has been refuted across the threshold of consecutive cycles while git still
    contradicts the claim, this hands the dispute **up** to Thomas exactly once
    and records a durable ``pr_number``-keyed standdown in the same ledger, so
    narration cannot overrule git indefinitely.

    Inside one atomic :meth:`EscalationLedgerBackend.mutate` closure (the JSON
    adapter's :func:`~maestro.fileutil.file_lock` span, or one ``BEGIN
    IMMEDIATE`` transaction on SQLite): if a standdown is already recorded for
    *pr_number*, returns ``False`` (no-op — the map is unchanged, so nothing is
    written). Otherwise appends one merge-or-close
    :class:`~maestro.messaging.Message` (``sender="maestro"``) to
    ``messages.jsonl`` via a :class:`~maestro.messaging.MessageStore`, records the
    durable standdown (with an empty ``head_sha`` — this escalation is keyed on
    ``pr_number`` alone, not a head — plus the delivery timestamp), and returns
    ``True``.

    Reusing the shared ledger means the standdown both suppresses further
    remediation minting (via :func:`has_standdown` / the ``escalation_bridge``
    task gate) and is auto-released by :func:`prune_resolved_standdowns` — already
    wired in :mod:`maestro.loop.pr_remediation` — when git shows the PR
    merged/closed. The whole read-check-write runs inside one ``mutate`` closure,
    so repeated calls are idempotent: only the first writes a message.

    *backend* is the injected :class:`EscalationLedgerBackend` (proposal 106).
    *message_store* lets a caller inject a store (tests); ``None`` builds one
    rooted at *data_dir*. *now* injects the ISO timestamp; ``None`` stamps the
    current UTC time.
    """
    def _apply(standdowns: dict[int, StanddownRecord]) -> bool:
        if pr_number in standdowns:
            return False
        store = message_store if message_store is not None else MessageStore(
            data_dir,
        )
        timestamp = now if now is not None else datetime.now(UTC).isoformat()
        msg = Message(
            message_id=f"reality-refutation-{pr_number}",
            timestamp=timestamp,
            sender="maestro",
            content=_reality_refutation_message(pr_number, refute_count),
            message_type="question",
        )
        store.save_message(msg)
        # Seed the serviced set empty: record-time review threads (if any) are
        # intentionally left un-serviced so each still earns one lap (#2798).
        standdowns[pr_number] = {
            "head_sha": "",
            "delivered_at": timestamp,
            "serviced_comment_ids": [],
            "claim": "reality-refuted",
        }
        logger.info(
            "escalation-bridge: delivered reality-refutation handoff to Thomas "
            "for git-unmerged PR #%d (refuted x%d); standdown recorded",
            pr_number,
            refute_count,
        )
        return True

    return backend.mutate(_apply)


class ClaimedEscalationDelivery(NamedTuple):
    """Outcome of a :func:`deliver_claimed_escalation` attempt.

    ``delivered`` is bound to a *post-write re-read* of the standdown ledger, not
    the send-call return: it is ``True`` only when a row keyed to the target
    number is present on disk *after* the write attempt. This closes the same
    null-act-stamps-success hole the ``mark_plan_obsolete`` re-read invariant
    closes — record the re-read status, not the call return — so a failed or lost
    row write cannot masquerade as a delivered escalation.

    ``reason`` carries a bracketed diagnostic tag on the non-delivered paths and
    is empty (``""``) on success:

    * ``[already-stood-down]`` — the idempotent no-op: a standdown for the
      number was already on record, so nothing was written or sent.
    * ``[row-missing]`` — the message was sent but the post-write re-read found
      no keyed row (a failed/lost write); ``delivered`` is ``False``, never a
      silent success.
    """

    delivered: bool
    reason: str


def deliver_claimed_escalation(
    data_dir: Path,
    issue_number: int,
    lap_count: int,
    *,
    backend: EscalationLedgerBackend,
    message_store: MessageStore | None = None,
    now: str | None = None,
) -> ClaimedEscalationDelivery:
    """Deliver one claimed-escalation knock for *issue_number*; return the outcome.

    Proposal 090's actuator — the phantom-escalation (rule 12) completion arm.
    When a ``partial`` remediation lap has narrated "escalating" for an
    issue/PR number across the ≥2-lap threshold with no outbound message
    referencing it, this *completes* the claimed escalation: it hands the
    number up to Thomas exactly once and records a durable key-keyed standdown,
    so the narration's promised action is delivered rather than re-litigated.
    The delivered message names ``#{issue_number}`` so
    :func:`~maestro.audit_checks.extract_issue_numbers` recovers it and rule 12
    self-extinguishes the next cycle; its id folds the delivery timestamp in via
    :func:`_claimed_escalation_message_id` so a re-escalation after
    :func:`prune_expired_standdowns` ages the record out gets a distinct id.

    Inside one atomic :meth:`EscalationLedgerBackend.mutate` closure (the JSON
    adapter's :func:`~maestro.fileutil.file_lock` span, or one ``BEGIN
    IMMEDIATE`` transaction on SQLite): if a standdown is already recorded for
    *issue_number*, the closure makes no change and this returns
    ``ClaimedEscalationDelivery(False, "[already-stood-down]")`` (no-op — nothing
    is written). Otherwise it appends one merge-or-close
    :class:`~maestro.messaging.Message` (``sender="maestro"``) via a
    :class:`~maestro.messaging.MessageStore`, and records the durable standdown
    with an empty ``head_sha``, an empty ``serviced_comment_ids``, the delivery
    timestamp, and an ``expires_at`` of ``delivered_at +
    _CLAIMED_ESCALATION_EXPIRY``.

    **Delivery-truth bind.** ``delivered`` is not stamped from the ``mutate``
    return: after the closure runs, the ledger is *re-read through the backend*
    (:meth:`EscalationLedgerBackend.read`) and ``delivered`` is ``True`` only
    when a row keyed to *issue_number* is present — matching the
    ``deliver_handoff`` re-read bind and the record-time outcome-binding
    discipline, now through the seam. A failed/lost row write yields
    ``ClaimedEscalationDelivery(False, "[row-missing]")`` even though the message
    was sent, never a silent success.

    Rule 12 keys on :func:`~maestro.audit_checks.extract_issue_numbers`, which
    can cite a plain issue rather than a PR. A standdown keyed on a plain issue
    can never be released by :func:`prune_resolved_standdowns` (an issue never
    "merges as a PR"), so this actuator's records carry ``expires_at`` and are
    aged out by :func:`prune_expired_standdowns`, keeping the ledger bounded.

    *backend* is the injected :class:`EscalationLedgerBackend` (proposal 106).
    *message_store* lets a caller inject a store (tests); ``None`` builds one
    rooted at *data_dir*. *now* injects the ISO timestamp; ``None`` stamps the
    current UTC time; an injected *now* must be timezone-aware — a malformed or
    naive value raises a ``ValueError`` naming the parameter before anything is
    written (:func:`_require_aware_timestamp`). The write runs inside one
    ``mutate`` closure, so repeated calls are idempotent: only the first writes
    a message.
    """
    timestamp = now if now is not None else datetime.now(UTC).isoformat()
    # Validate before the ``mutate`` closure runs, so a rejected timestamp
    # writes neither a message nor a ledger row: a naive deadline would be
    # dropped by the loader and leave an issue-keyed record no pruner can
    # release (see :func:`_require_aware_timestamp`).
    expires_at = (
        _require_aware_timestamp(timestamp, "now") + _CLAIMED_ESCALATION_EXPIRY
    ).isoformat()

    def _apply(standdowns: dict[int, StanddownRecord]) -> bool:
        if issue_number in standdowns:
            return False
        store = message_store if message_store is not None else MessageStore(
            data_dir,
        )
        msg = Message(
            message_id=_claimed_escalation_message_id(issue_number, timestamp),
            timestamp=timestamp,
            sender="maestro",
            content=_claimed_escalation_message(issue_number, lap_count),
            message_type="question",
        )
        store.save_message(msg)
        # Seed the serviced set empty (#2798); carry the age-based expiry so the
        # issue-keyed record is bounded even though the merge/close pruner can
        # never release a plain issue number.
        standdowns[issue_number] = {
            "head_sha": "",
            "delivered_at": timestamp,
            "serviced_comment_ids": [],
            "expires_at": expires_at,
            "claim": "escalation-claimed",
        }
        return True

    if not backend.mutate(_apply):
        return ClaimedEscalationDelivery(False, "[already-stood-down]")
    # Delivery-truth bind: re-read the ledger through the backend and confirm a
    # row keyed to *issue_number* is present. Binding delivered to the re-read —
    # not the mutate return — means a failed/lost row write is reported as a
    # non-delivery ([row-missing]) rather than a phantom success, even though the
    # message-send path already ran.
    if issue_number not in backend.read():
        logger.warning(
            "escalation-bridge: claimed-escalation message sent for #%d but "
            "no standdown row present on post-write re-read; reporting "
            "not-delivered [row-missing]",
            issue_number,
        )
        return ClaimedEscalationDelivery(False, "[row-missing]")
    logger.info(
        "escalation-bridge: delivered claimed-escalation handoff to Thomas "
        "for #%d (narrated escalating x%d); standdown recorded (expires %s)",
        issue_number,
        lap_count,
        expires_at,
    )
    return ClaimedEscalationDelivery(True, "")


def deliver_handoff_cfg(
    config: Config,
    handoff: UnlandedCureHandoff,
    *,
    backend: EscalationLedgerBackend,
    message_store: MessageStore | None = None,
    now: str | None = None,
    plan_lookup: Callable[[int], PlanCapability | None] | None = None,
    claim: StanddownClaim,
) -> bool:
    """:func:`deliver_handoff` with the refuted-release budget already wired.

    The config-aware door every production handoff site uses (proposal 135).
    :func:`deliver_handoff` stays the mechanism — it takes a ``data_dir`` and an
    explicit *release_cap*, so tests can drive it with any budget — while this
    thin wrapper is the one place that derives both arguments from *config*:
    ``config.memory_dir`` and the single-owner
    :attr:`~maestro.config.Config.active_refutation_release_cap` (``0`` while
    ``refutable_standdown_enabled`` is off, which skips the probe entirely).

    Routing every site through here is what keeps the
    ``StanddownRecord.refutation_releases`` projection honest: a new handoff site
    that forgets to thread the budget by hand would silently write records that
    never show how many times the PR's latch has already been taken back, and
    the omission would be invisible — the field is
    :class:`~typing.NotRequired`. Passing a config instead of a cap makes the
    omission impossible to express.
    """
    return deliver_handoff(
        config.memory_dir,
        handoff,
        backend=backend,
        message_store=message_store,
        now=now,
        plan_lookup=plan_lookup,
        release_cap=config.active_refutation_release_cap,
        claim=claim,
    )


def deliver_handoff(
    data_dir: Path,
    handoff: UnlandedCureHandoff,
    *,
    backend: EscalationLedgerBackend,
    message_store: MessageStore | None = None,
    now: str | None = None,
    plan_lookup: Callable[[int], PlanCapability | None] | None = None,
    message: str | None = None,
    release_cap: int = 0,
    claim: StanddownClaim,
) -> bool:
    """Deliver *handoff* to Thomas exactly once; return whether it delivered.

    Inside one atomic :meth:`EscalationLedgerBackend.mutate` closure (the JSON
    adapter's :func:`~maestro.fileutil.file_lock` span, or one ``BEGIN
    IMMEDIATE`` transaction on SQLite): if a ``pr_number``-keyed standdown is
    already recorded for the handoff's PR, returns ``False`` (no-op — the map is
    unchanged, so nothing is written). Otherwise appends one merge-or-close
    :class:`~maestro.messaging.Message` (``sender="maestro"``) to
    ``messages.jsonl`` via a :class:`~maestro.messaging.MessageStore`, records
    the durable standdown (with the head SHA as an informational field), and
    returns ``True``.

    *backend* is the injected :class:`EscalationLedgerBackend` (proposal 106).
    *message_store* lets a caller inject a store (tests); ``None`` builds one
    rooted at *data_dir*. *now* injects the ISO timestamp; ``None`` stamps the
    current UTC time. *message* overrides the rendered body — ``None`` renders
    the *claim*'s default: :func:`_ready_handoff_message` for ``HANDOFF_READY``,
    :func:`_handoff_message` for ``LAP_BUDGET_EXHAUSTED``; the only current
    override is
    :func:`ci_pending_green_handoff_message` (#3630), whose premise is "locally
    green with CI still pending". The standdown, its key, and the exactly-once
    semantics are identical either way.
    The whole read-check-write runs inside one ``mutate``
    closure, so repeated calls are idempotent regardless of head changes: only
    the first writes a message, and a later commit on the same still-unmerged PR
    is suppressed (proposal 080 Phase 3).

    *plan_lookup* resolves the handoff's ``pr_number`` to its owning
    :class:`PlanCapability` (typically ``_plan_for_pr_number``). When supplied
    and it resolves a plan, the composer prepends one bounded capability line
    naming what the merge would land (:func:`_capability_line`); ``None`` — or a
    lookup returning ``None`` — leaves the message byte-identical to the generic
    merge-or-close text. The lookup runs only on the fresh-delivery path (after
    the standdown-presence check), so the idempotent no-op pays no plan read.

    *release_cap* bounds the :func:`count_refutation_releases` probe stamped onto
    the new record as ``refutation_releases`` (proposal 135), so a record written
    after one or more refuted releases shows how many times this PR's latch has
    already been taken back. It is inspection-only — the cap decision itself
    reads the message ledger, never this field — and the default ``0`` skips the
    probe entirely, leaving every existing caller byte-identical.

    *claim* is the premise the caller acted on, persisted as the record's
    ``claim`` (#4110). It is required, with no default, so a new caller cannot
    silently inherit the wrong premise: a readiness site ("only Thomas can act
    now") stamps :attr:`~maestro.standdown_validity.StanddownClaim.HANDOFF_READY`,
    which a live ``CODE_OWED`` reading refutes; an exhaustion site ("the lap /
    no-op budget was spent") stamps
    :attr:`~maestro.standdown_validity.StanddownClaim.LAP_BUDGET_EXHAUSTED`,
    under which owned work is expected, so the latch holds.
    """
    def _apply(standdowns: dict[int, StanddownRecord]) -> bool:
        if handoff.pr_number in standdowns:
            return False
        store = message_store if message_store is not None else MessageStore(
            data_dir,
        )
        timestamp = now if now is not None else datetime.now(UTC).isoformat()
        plan = (
            plan_lookup(handoff.pr_number) if plan_lookup is not None else None
        )
        msg = Message(
            message_id=_fresh_handoff_message_id(
                store, handoff.pr_number, handoff.head_sha,
            ),
            timestamp=timestamp,
            sender="maestro",
            content=(
                message if message is not None
                else _ready_handoff_message(handoff, plan)
                if claim is StanddownClaim.HANDOFF_READY
                else _handoff_message(handoff, plan)
            ),
            message_type="question",
        )
        store.save_message(msg)
        # Seed the serviced set empty: record-time review threads (if any) are
        # intentionally left un-serviced so each still earns one lap (#2798).
        record: StanddownRecord = {
            "head_sha": handoff.head_sha,
            "delivered_at": timestamp,
            "serviced_comment_ids": [],
            "claim": claim.value,
        }
        if release_cap > 0:
            # Read-only projection of the durable withdrawal-message count, for
            # inspection of a PR that has already been handed off and refuted.
            record["refutation_releases"] = count_refutation_releases(
                store, handoff.pr_number, cap=release_cap,
            )
        standdowns[handoff.pr_number] = record
        logger.info(
            "escalation-bridge: delivered merge-or-close handoff to Thomas "
            "for unlanded PR #%d (claim %s); standdown recorded",
            handoff.pr_number,
            claim.value,
        )
        return True

    return backend.mutate(_apply)


def deliver_retirement_disposition(
    data_dir: Path,
    pr_number: int,
    *,
    backend: EscalationLedgerBackend,
    plan_title: str,
    pr_url: str,
    comment_poster: Callable[[int, str], bool],
    message_store: MessageStore | None = None,
    now: str | None = None,
) -> bool:
    """Deliver a retirement disposition for *pr_number*; return whether retirement may proceed.

    Proposal 091's gate on :meth:`~maestro.memory.Memory.sweep_orphaned_cures`:
    the mechanical orphaned-cure sweep may retire an ``implemented``-but-unmerged
    plan whose PR is still open **only** after an explicit disposition has been
    delivered to both the PR and Thomas. Modeled on :func:`deliver_handoff` and
    reusing the same ``pr_number``-keyed standdown ledger, so the recorded record
    both suppresses further remediation minting and is auto-released by
    :func:`prune_resolved_standdowns` when the host reports the PR merged/closed.

    Idempotency is keyed on the retirement disposition **itself** — whether the
    ``disposition-<pr>`` :class:`~maestro.messaging.Message` is already on record —
    not merely on a ``pr_number``-keyed standdown. A prior escalation deliverer
    (:func:`deliver_handoff` / :func:`deliver_reality_refutation` /
    :func:`deliver_claimed_escalation`) records a standdown but posts **no** on-PR
    comment; gating on standdown presence would let retirement proceed with the
    close-or-keep PR breadcrumb never landing. Gating on the disposition message
    instead guarantees the PR comment is posted exactly once per PR — regardless of
    any prior standdown — while a re-run never double-posts.

    Under the standdown-ledger :func:`~maestro.fileutil.file_lock`:

    * If the ``disposition-<pr>`` message is already on record, returns ``True``
      without re-posting — the retirement disposition was already delivered (its
      PR comment landed), so retirement may proceed (idempotent). The re-entry
      is self-healing: if the ``pr_number`` standdown is absent (a prior cycle
      durably appended the message but its ledger persist failed after the
      closure), it backfills the seed record — carrying the message's own
      delivery timestamp — before returning, so the "a standdown is on record"
      invariant this path asserts is the invariant it enforces.
    * Otherwise posts the close-or-keep PR comment via *comment_poster* **first**.
      Only on a truthy post does it append one close-or-keep
      :class:`~maestro.messaging.Message` (``sender="maestro"``,
      ``message_type="question"``) via a :class:`~maestro.messaging.MessageStore`,
      record a ``pr_number``-keyed standdown **when one is not already present**
      (an existing standdown — e.g. a prior handoff's, with its own serviced set /
      head — is preserved, not clobbered; new records seed empty
      ``serviced_comment_ids`` and no ``expires_at`` — merge/close-only release),
      and return ``True``.
    * If the comment post returns falsy, writes **nothing** and returns ``False``
      (fail-closed): the caller must leave the plan ``implemented`` so tend keeps
      servicing it — nothing is orphaned.

    The delivered message id is ``disposition-<pr>`` (see
    :func:`_retirement_disposition_message_id`), distinct from the
    ``handoff-<pr>-…`` family so the handoff-relapse sensor never miscounts it.

    *backend* is the injected :class:`EscalationLedgerBackend` (proposal 106).
    *comment_poster* posts the PR comment (a PR is an issue on GitHub) and returns
    ``bool`` for clean gating. *message_store* lets a caller inject a store
    (tests); ``None`` builds one rooted at *data_dir*. *now* injects the ISO
    timestamp; ``None`` stamps the current UTC time. The whole
    read-check-post-write runs inside one ``mutate`` closure, so repeated calls
    are idempotent.

    Every ``True`` return is bound to a post-write ledger re-read (mirroring
    :func:`deliver_claimed_escalation`): if no ``pr_number`` row is present after
    the write, the delivery is reported not-delivered (``False``, fail-closed —
    the plan stays ``implemented`` for tend) rather than a phantom retirement.
    """
    disposition_id = _retirement_disposition_message_id(pr_number)

    def _apply(standdowns: dict[int, StanddownRecord]) -> bool:
        store = message_store if message_store is not None else MessageStore(
            data_dir,
        )
        thread = store.get_thread(disposition_id)
        if thread:
            # The retirement disposition message is already on record — its PR
            # comment landed. Self-heal the second leg rather than assume it:
            # if the ``pr_number`` standdown is absent (a prior cycle durably
            # appended the message but its ledger persist failed after the
            # closure returned), backfill the same seed the fresh path writes so
            # the "a standdown is on record" invariant this branch asserts is
            # the invariant it enforces. ``delivered_at`` is the true delivery
            # time carried on the message already in hand, not a repair-time
            # stamp. Mutating the map is enough to persist it (``mutate`` writes
            # iff the map changed); when the row is already present the map is
            # unchanged and nothing is re-posted or re-written. Idempotent:
            # retirement may proceed.
            if pr_number not in standdowns:
                standdowns[pr_number] = {
                    "head_sha": "",
                    "delivered_at": thread[0].timestamp,
                    "serviced_comment_ids": [],
                    "claim": "retirement-pending",
                }
            return True
        if not comment_poster(
            pr_number, _retirement_pr_comment(pr_number, plan_title),
        ):
            # Fail-closed: the PR comment did not post, so no disposition was
            # delivered. The map is unchanged, so nothing is written; refuse the
            # retirement.
            return False
        timestamp = now if now is not None else datetime.now(UTC).isoformat()
        msg = Message(
            message_id=disposition_id,
            timestamp=timestamp,
            sender="maestro",
            content=_retirement_thomas_message(pr_number, plan_title, pr_url),
            message_type="question",
        )
        store.save_message(msg)
        if pr_number not in standdowns:
            # Seed the serviced set empty and set no ``expires_at``: this is a
            # PR-keyed record released only on merge/close by
            # ``prune_resolved_standdowns``. A prior deliverer's standdown is
            # left intact — presence is what gates minting, and the
            # ``disposition-<pr>`` message is the durable retirement breadcrumb.
            # When one is already present the map is unchanged and only the
            # disposition message (saved above) lands — matching the historical
            # "write nothing to the ledger when a standdown already exists".
            standdowns[pr_number] = {
                "head_sha": "",
                "delivered_at": timestamp,
                "serviced_comment_ids": [],
                "claim": "retirement-pending",
            }
        logger.info(
            "escalation-bridge: delivered retirement disposition to Thomas and "
            "PR #%d (plan %r); standdown recorded, retirement may proceed",
            pr_number,
            plan_title,
        )
        return True

    delivered = backend.mutate(_apply)
    if not delivered:
        # Fail-closed comment-post path: nothing was written, so there is no
        # standdown to confirm.
        return False
    # Delivery-truth bind: re-read the ledger through the backend and confirm a
    # row keyed to *pr_number* is present. Binding the ``True`` return to the
    # re-read — not the ``mutate`` result — means a silently dropped ledger
    # write is reported as a non-delivery (fail-closed: the plan stays
    # ``implemented`` so tend keeps servicing the PR) rather than a phantom
    # retirement of a plan whose still-open PR has no standdown. Mirrors
    # :func:`deliver_claimed_escalation`'s post-write ``[row-missing]`` bind.
    if pr_number not in backend.read():
        logger.warning(
            "escalation-bridge: retirement disposition delivered for PR #%d "
            "but no standdown row present on post-write re-read; reporting "
            "not-delivered [row-missing]",
            pr_number,
        )
        return False
    return True


class UnwinnableRaceDelivery(NamedTuple):
    """Outcome of a :func:`deliver_unwinnable_race` attempt.

    ``delivered`` is bound to a *post-write re-read* of the message thread, not
    the ``save_message`` return: it is ``True`` only when the
    ``unwinnable-race-<pr>`` thread is present on disk *after* the write attempt.
    This mirrors :class:`ClaimedEscalationDelivery`'s re-read bind — record the
    re-read status, not the call return — so a failed/lost message write cannot
    masquerade as a delivered escalation.

    Unlike every sibling deliverer this one writes **no** stand-down, so the PR
    is never muted: idempotency lives entirely in the message thread.

    ``reason`` carries a bracketed diagnostic tag on the non-delivered paths and
    is empty (``""``) on success:

    * ``[already-delivered]`` — the idempotent no-op: the
      ``unwinnable-race-<pr>`` thread was already on record, so nothing was
      written or sent.
    * ``[message-missing]`` — the message was sent but the post-write re-read
      found no thread (a failed/lost write); ``delivered`` is ``False``, never a
      silent success.
    """

    delivered: bool
    reason: str


def _unwinnable_race_message_id(pr_number: int) -> str:
    """Return the message id for a *pr_number* unwinnable-race heads-up.

    Shaped ``unwinnable-race-<pr>`` — deliberately **not** the
    ``handoff-<pr>-<head_tag>`` family (:func:`_message_id`), so
    :data:`~maestro.remediation_tracker._HANDOFF_MESSAGE_ID_PATTERN`
    (``^handoff-(\\d+)-``) never miscounts this heads-up as an unlanded-cure
    handoff relapse. Because this deliverer records **no** stand-down, the
    message thread is the escalation's *only* durable trace, so the id is keyed
    on ``pr_number`` alone and stays stable across laps: that stability is what
    lets the :meth:`~maestro.messaging.MessageStore.get_thread` idempotency check
    suppress re-delivery. Registered in
    :data:`~maestro.audit_checks._HANDOFF_MESSAGE_ID_PREFIXES` so
    :func:`~maestro.audit_checks.check_banked_owned_act_reperformance` recognises
    the delivered act.
    """
    return f"unwinnable-race-{pr_number}"


def _unwinnable_race_message(pr_number: int, re_entry_count: int) -> str:
    """Render the unwinnable-race heads-up addressed to Thomas.

    States the PR, the re-entry count, that the race against a moving
    ``origin/main`` is being lost, and — unlike every sibling escalation — that
    the loop is **not** standing down: it will keep re-entering the race, so
    Thomas may want to intervene (merge the PR, or resolve the hotspot that keeps
    moving the base). Names ``#{pr_number}`` so
    :func:`~maestro.audit_checks.extract_issue_numbers` recovers it.
    """
    return (
        f"Unwinnable-race — I have re-entered the conflict-resolution race for "
        f"PR #{pr_number} {re_entry_count} times as origin/main kept moving "
        f"under it, and it still has not landed. The base is advancing faster "
        f"than one remediation lap resolves the conflict, so I keep losing the "
        f"same race. I am **not** standing down: PR #{pr_number} stays fully "
        f"active and I will keep re-entering the race. But you may want to "
        f"intervene — merge #{pr_number} yourself, or resolve the hotspot that "
        f"keeps moving the base — since I cannot win a race the repository "
        f"re-runs every lap."
    )


def _refutation_withdrawal_message_id(pr_number: int, release_index: int) -> str:
    """Return the message id for the *release_index*-th refuted-release retraction.

    Shaped ``standdown-withdrawn-<pr>-<n>`` — deliberately **not** the
    ``handoff-<pr>-<head_tag>`` family (:func:`_message_id`), so
    :data:`~maestro.remediation_tracker._HANDOFF_MESSAGE_ID_PATTERN`
    (``^handoff-(\\d+)-``) never miscounts a withdrawal as an unlanded-cure
    handoff relapse — the same rationale
    :func:`_retirement_disposition_message_id` and
    :func:`_unwinnable_race_message_id` already carry.

    The 1-based *release_index* is folded in for the reason
    :func:`_claimed_escalation_message_id` folds in ``delivered_at``: the same PR
    may be handed off and refuted more than once, and a bare
    ``standdown-withdrawn-<pr>`` would collide across cycles — breaking
    :meth:`~maestro.messaging.MessageStore.mark_read` (which stops at the first
    id match) and conflating :meth:`~maestro.messaging.MessageStore.get_thread`
    (which returns every row sharing the id). That per-release distinctness is
    also what lets :func:`count_refutation_releases` use the durable message
    ledger as the release counter.
    """
    return f"standdown-withdrawn-{pr_number}-{release_index}"


def _refutation_capped_message_id(pr_number: int) -> str:
    """Return the message id for *pr_number*'s terminal release-cap notice.

    Shaped ``standdown-refutation-capped-<pr>``, disjoint from the
    ``handoff-<pr>-…`` family for the same relapse-miscount reason as its two
    sibling helpers. Keyed on ``pr_number`` alone and stable across laps: the
    cap is terminal, so exactly one such message is ever owed per PR and the
    :meth:`~maestro.messaging.MessageStore.get_thread` probe on this stable id is
    what makes repeated tend passes over a capped PR write nothing. Registered
    in :data:`~maestro.audit_checks._HANDOFF_MESSAGE_ID_PREFIXES` — unlike the
    withdrawal id — because a terminal cap notice *is* a durable merge-or-close
    escalation, whereas a withdrawal is the *un*-banking of one.
    """
    return f"standdown-refutation-capped-{pr_number}"


def _refutation_withdrawal_message(
    pr_number: int, blocker: str, owed_act: str,
) -> str:
    """Render the retraction of a refuted stand-down, addressed to Thomas.

    The handoff message told Thomas that only he could land this PR and that the
    loop was standing down. The refuted release has now measured that claim to be
    false, so it is taken back out loud rather than silently deleted from the
    ledger while the announcement sits unretracted in his inbox. Names the PR as
    ``#{pr_number}`` (so :func:`~maestro.audit_checks.extract_issue_numbers`
    recovers it), the *blocker* that refuted the claim, and the owed act the live
    reading returned.
    """
    # ``MERGE_BLOCK_NONE`` is the "no deterministic merge blocker" sentinel, not
    # a reason: a latch is also refuted by an unresolved review thread, which
    # ``_observe_owed_act`` reports as ``code-owed`` with that sentinel. Printing
    # "NONE" would hand Thomas a meaningless word where the message promises a
    # concrete reason, so the sentinel falls through to the generic phrasing.
    reason = (
        blocker
        if blocker and blocker != MERGE_BLOCK_NONE
        else "a newly measured blocker"
    )
    act = owed_act or "owned code work"
    return (
        f"Withdrawing my stand-down — I told you only you could land PR "
        f"#{pr_number}, and that claim no longer holds: the live reading now "
        f"shows {reason}, so the owed act is {act} — mine, not yours. I am "
        f"taking the stand-down back and resuming remediation laps for PR "
        f"#{pr_number}. No action is owed by you on that earlier handoff."
    )


def _refutation_capped_message(pr_number: int, cap: int) -> str:
    """Render the terminal release-cap notice, addressed to Thomas.

    Sent exactly once per PR, when its ``handoff-ready`` latch has already been
    released by refutation *cap* times. Past that budget the latch becomes
    permanent: without a bound a PR flapping between merge-ready and
    ``CONFLICTING`` would cycle handoff → refutation → handoff forever, trading a
    silent deadlock for an unbounded message stream. Names ``#{pr_number}`` so
    the PR surfaces as a bounded, named failure.
    """
    return (
        f"Stand-down for PR #{pr_number} is now permanent — I have withdrawn and "
        f"re-asserted it {cap} time(s), which is its whole release budget "
        f"([loop] standdown_refutation_release_cap). It keeps flipping between "
        f"merge-ready and blocked faster than one remediation lap resolves, so "
        f"I am not going to keep re-litigating it: no further remediation laps "
        f"will be minted for PR #{pr_number} until it merges or closes. Please "
        f"merge #{pr_number} or close/retire it."
    )


def count_refutation_releases(
    store: MessageStore, pr_number: int, *, cap: int,
) -> int:
    """Return how many refuted releases for *pr_number* are already on record.

    The durable message ledger **is** the release counter, and this is the single
    authority for the cap. It probes
    ``store.get_thread(_refutation_withdrawal_message_id(pr_number, n))`` for
    ``n = 1 … cap`` and returns the count of ids present, stopping at the first
    gap. :meth:`~maestro.messaging.MessageStore.get_thread` truthiness is the
    house existence idiom (:func:`deliver_retirement_disposition`,
    :func:`deliver_unwinnable_race`), and the probe is bounded by *cap* so the
    read cost is fixed.

    Counting the *messages* rather than a field on the stand-down record is the
    point: the release itself **deletes** that record, so any counter stamped on
    it is destroyed by the very event it counts, while ``messages.jsonl`` is
    append-only and survives every release. (:attr:`StanddownRecord`'s
    ``refutation_releases`` is a read-only projection of this count for
    inspection, never the authority.)
    """
    seen = 0
    for index in range(1, cap + 1):
        if not store.get_thread(
            _refutation_withdrawal_message_id(pr_number, index),
        ):
            break
        seen += 1
    return seen


def deliver_unwinnable_race(
    data_dir: Path,
    pr_number: int,
    re_entry_count: int,
    *,
    backend: EscalationLedgerBackend,
    message_store: MessageStore | None = None,
    now: str | None = None,
) -> UnwinnableRaceDelivery:
    """Deliver one unwinnable-race heads-up for *pr_number*; return the outcome.

    Proposal 099's message-only concession (step 8). When a conflict-remediation
    lineage has re-entered the resolution race on base advance beyond the
    configured threshold without ever landing, this hands the fact up to Thomas
    exactly once — a heads-up, not a give-up.

    Unlike every sibling deliverer (:func:`deliver_handoff`,
    :func:`deliver_reality_refutation`, :func:`deliver_claimed_escalation`,
    :func:`deliver_retirement_disposition`), this writes **no** stand-down: the
    PR is never muted, so the loop keeps re-entering the race after the human is
    told. Idempotency therefore lives entirely in the message thread —
    ``unwinnable-race-<pr>``, the escalation's only durable trace — and the
    ``escalation_handoffs.json`` ledger stays untouched.

    Inside one atomic :meth:`EscalationLedgerBackend.mutate` closure — shared
    with the sibling deliverers so a concurrent standdown write cannot interleave
    (the JSON adapter's :func:`~maestro.fileutil.file_lock` span, or one ``BEGIN
    IMMEDIATE`` transaction on SQLite) — even though this deliverer records no
    standdown, so the closure never changes the map and the backend writes
    nothing to the ledger (the ``escalation_handoffs.json`` / ``state.db``
    standdown store stays untouched):

    * If the ``unwinnable-race-<pr>`` thread is already on record, returns
      ``UnwinnableRaceDelivery(False, "[already-delivered]")`` — the idempotent
      no-op, nothing written or sent.
    * Otherwise appends one :class:`~maestro.messaging.Message`
      (``sender="maestro"``, ``message_type="question"``) via a
      :class:`~maestro.messaging.MessageStore`, then re-reads the thread.

    **Delivery-truth bind.** ``delivered`` is not stamped from the
    ``save_message`` return: after the closure runs the thread is *re-read from
    disk* and ``delivered`` is ``True`` only when the ``unwinnable-race-<pr>``
    thread is present — matching :class:`ClaimedEscalationDelivery`'s re-read
    bind. A failed/lost write yields
    ``UnwinnableRaceDelivery(False, "[message-missing]")`` even though the send
    path ran, never a silent success.

    *backend* is the injected :class:`EscalationLedgerBackend` (proposal 106).
    *message_store* lets a caller inject a store (tests); ``None`` builds one
    rooted at *data_dir*. *now* injects the ISO timestamp; ``None`` stamps the
    current UTC time. The whole read-check-write runs inside one ``mutate``
    closure, so repeated calls are idempotent: only the first writes a message.
    """
    message_id = _unwinnable_race_message_id(pr_number)
    store = message_store if message_store is not None else MessageStore(
        data_dir,
    )

    def _apply(standdowns: dict[int, StanddownRecord]) -> bool:
        if store.get_thread(message_id):
            # Idempotent no-op: the heads-up thread already exists. Nothing is
            # written or sent — and because this deliverer records no standdown,
            # the message thread is the sole idempotency key.
            return False
        timestamp = now if now is not None else datetime.now(UTC).isoformat()
        msg = Message(
            message_id=message_id,
            timestamp=timestamp,
            sender="maestro",
            content=_unwinnable_race_message(pr_number, re_entry_count),
            message_type="question",
        )
        store.save_message(msg)
        return True

    if not backend.mutate(_apply):
        return UnwinnableRaceDelivery(False, "[already-delivered]")
    # Delivery-truth bind: re-read the thread from disk and confirm it is
    # present. Binding delivered to the re-read — not the save-call return —
    # means a failed/lost write is reported as a non-delivery
    # ([message-missing]) rather than a phantom success, even though the
    # send path already ran.
    if not store.get_thread(message_id):
        logger.warning(
            "escalation-bridge: unwinnable-race message sent for #%d but no "
            "thread present on post-write re-read; reporting not-delivered "
            "[message-missing]",
            pr_number,
        )
        return UnwinnableRaceDelivery(False, "[message-missing]")
    logger.info(
        "escalation-bridge: delivered unwinnable-race heads-up to Thomas "
        "for PR #%d (re-entered the base-advance race x%d, still unlanded); "
        "no standdown recorded — PR stays routable",
        pr_number,
        re_entry_count,
    )
    return UnwinnableRaceDelivery(True, "")
