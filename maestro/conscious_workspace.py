"""Conscious-access workspace — the leaf owner of Maestro's bounded, provenance-
bearing record of *what is present to the loop right now* (proposal 130).

Maestro already owns the raw material a global workspace would need —
``diagnostics.DiagnosticSnapshot`` and ``audit_checks.AuditFinding`` produce
structured observations of its own operating state, ``journal`` holds subjective
narrative — but none of it is assembled
into a single capacity-limited record that names *why* a fact was admitted and
another excluded, carries a stable identity hash, and links to the moment before
it. This module is the closed vocabulary, the frozen record shapes, the hard
content bounds, the moment hashes, and the append-only episode ledger those
future increments key against. It lands the schema first, deliberately
behavior-inert: nothing here selects, broadcasts, or writes an episode yet, so
the on-disk shape is settled before receipts, citations, and indicator readings
all key to it (mirroring how :mod:`maestro.plan_grounding` and
:mod:`maestro.gate_capacity` landed as owner-only foundations).

Three permanent boundaries this owner enforces:

* **The ledger is append-only.** One episode per row, never mutated in place;
  reads walk newest-first with a bounded corrupt-record skip so a corrupted tail
  can never force a full-file scan.
* **No raw reasoning and no credential may ever enter a record.** Every item is
  built through :func:`build_workspace_item`, which head-truncates its ``summary``
  to a hard cap and *refuses* — raising :class:`ValueError` — any summary that
  trips :func:`maestro.secret_scan.has_hard_block`, so a credential can never be
  persisted.
* **The claim vocabulary never collapses.** These records implement *conscious
  access* (a limited set of current information is selected and made available),
  never phenomenal consciousness. See ``docs/conscious-access.md``.

This is a **leaf module**: it imports only the standard library plus
:mod:`maestro.fileutil` and :mod:`maestro.secret_scan`, so
:mod:`maestro.state_store` can import it for the SQLite adapter without an import
cycle (nothing in a leaf may import ``state_store``). Mirrors the
:mod:`maestro.gate_observation` / :mod:`maestro.plan_grounding` skeleton — a
``Literal`` closed vocabulary, ``@dataclass(frozen=True)`` provenance-carrying
records, and the :mod:`maestro.directive_ledger` explicit serialize-out /
explicit tuple-coerce-in round-trip for its nested records.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast, get_args, runtime_checkable

from maestro.fileutil import file_lock, iter_jsonl_reverse
from maestro.secret_scan import has_hard_block

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The closed vocabularies
# ---------------------------------------------------------------------------

#: How far a workspace item's provenance is trusted. ``authenticated_human`` is a
#: directive from the authorized operator; ``mechanical_observation`` is a
#: zero-SDK git/gate fact; ``agent_derived`` is the model's own inference; and
#: ``untrusted_external`` is anything sourced from outside the trust boundary.
#: Kept a ``Literal`` so a new class is a one-line vocabulary edit.
TrustClass = Literal[
    "authenticated_human",
    "mechanical_observation",
    "agent_derived",
    "untrusted_external",
]

#: The observation owners a workspace candidate may be adapted from — the
#: existing structured-observation producers a later selection policy draws from.
#: Kept a ``Literal`` so the candidate adapters the next increment builds must
#: name exactly one known owner.
SourceKind = Literal[
    "diagnostic_snapshot",
    "audit_finding",
    "journal_thought",
    "self_model",
    "human_directive",
    "task_outcome",
    "pr_focus",
    "queue_pressure",
    "budget_state",
    "prediction_verdict",
]

#: Why a candidate lost its bid for the capacity-limited workspace.
#: ``capacity_exceeded`` — the admitted set was already at ``max_items``;
#: ``duplicate_source`` — deduplicated against an already-admitted item;
#: ``expired`` — the candidate's ``expires_at`` had passed; ``low_score`` —
#: ranked below the admission cutoff; ``untrusted`` — its trust class was below
#: the admission floor. The exclusion trace is what makes the workspace's
#: *omissions* legible rather than silent.
ExclusionReason = Literal[
    "capacity_exceeded",
    "duplicate_source",
    "expired",
    "low_score",
    "untrusted",
]

#: How one moment links to its predecessor. ``genesis`` — the first moment, no
#: predecessor; ``linked`` — an ordinary successor of the moment before it;
#: ``crash_gap`` — the loop crashed between this moment and its predecessor;
#: ``disabled_interval`` — the workspace was switched off between the two.
#: Temporal continuity is an indicator the record must be able to represent even
#: before any writer stamps it.
ContinuityKind = Literal["genesis", "linked", "crash_gap", "disabled_interval"]

#: The reasoning phases a selected moment may be broadcast to (proposal 130).
#: ``contemplation`` and ``evolution`` are the two priced-introspection consumers
#: that both route through :meth:`maestro.consciousness.Consciousness.introspect`;
#: ``task_generation`` is the third consumer — the loop's candidate-task-queue
#: decision — which does **not** route through ``introspect`` but receives the
#: same rendered block by a per-call prepend in
#: :meth:`maestro.loop.task_generation.TaskGenerator.generate_task`.
#: ``task_execution`` is the fourth consumer — the task-execution planning
#: decision reached through :meth:`maestro.planner.Planner.decompose`, which turns
#: a goal into an executable step graph — and likewise receives the block by a
#: per-call prepend rather than through ``introspect``.
#: Kept a ``Literal`` so widening delivery to a new consumer is a one-line
#: vocabulary edit and every broadcast site must name exactly one known consumer.
ConsumerName = Literal[
    "contemplation", "evolution", "task_generation", "task_execution",
]

#: Who a moment may be *rendered for*: the four broadcast reasoning consumers
#: plus ``self_inspection`` — the read-only self-awareness tools (proposal 130
#: step 8, ``read_conscious_moment`` / ``read_conscious_access_profile``) that
#: render the current moment/profile for the agent's own inspection rather than
#: delivering it to a reasoning phase. Deliberately kept a *superset* of
#: :data:`ConsumerName` (which stays the closed *broadcast* vocabulary
#: :data:`CONSCIOUS_CONSUMERS` is derived from), so a self-inspection read is
#: never mistaken for a broadcast target and never earns a
#: :class:`BroadcastReceipt`. Redaction is consumer-invariant, so
#: ``self_inspection`` withholds exactly what every broadcast consumer does.
RenderConsumer = ConsumerName | Literal["self_inspection"]

#: The vocabularies as membership sets, derived from the aliases via ``get_args``
#: so each member is spelled exactly once. Read by the tolerant record parsers to
#: drop a row carrying a value outside the closed vocabulary.
_TRUST_CLASSES: frozenset[str] = frozenset(get_args(TrustClass))
_SOURCE_KINDS: frozenset[str] = frozenset(get_args(SourceKind))
_EXCLUSION_REASONS: frozenset[str] = frozenset(get_args(ExclusionReason))
_CONTINUITY_KINDS: frozenset[str] = frozenset(get_args(ContinuityKind))

#: The broadcast consumers as a membership set, derived from :data:`ConsumerName`
#: via ``get_args`` (mirroring the :data:`_TRUST_CLASSES` idiom) so each consumer
#: is spelled exactly once. Public because the broadcast site and its tests check
#: membership; the delivery seam never invents a consumer outside this set.
CONSCIOUS_CONSUMERS: frozenset[str] = frozenset(get_args(ConsumerName))

# ---------------------------------------------------------------------------
# Hard content bounds (context-bloat caps — this text reaches a prompt later)
# ---------------------------------------------------------------------------

#: Hard cap on a single item's ``summary``. Every admitted item's summary will
#: eventually be broadcast into cognitive sessions, so it carries the same
#: context-bloat discipline as :data:`maestro.gate_observation._MAX_MESSAGE_CHARS`:
#: enough to identify the fact, never enough to smuggle raw reasoning into the
#: workspace.
MAX_ITEM_SUMMARY_CHARS = 320

#: Hard cap on an :class:`AttentionSchema`'s ``focus`` free-text field. The
#: schema rides inside :func:`compute_selection_hash` and is the record most
#: destined for prompt broadcast, so its free-text fields carry the same
#: context-bloat discipline as the item summary. Enforced by
#: :func:`build_attention_schema`.
MAX_FOCUS_CHARS = 200

#: Hard cap on an :class:`AttentionSchema`'s ``permission_summary`` free-text
#: field, the access profile in force. Enforced by
#: :func:`build_attention_schema`.
MAX_PERMISSION_SUMMARY_CHARS = 200

#: Hard cap on the number of ``blind_spots`` phrases an :class:`AttentionSchema`
#: carries — a bounded self-model of known measurement gaps, not an exhaustive
#: log. Enforced by :func:`build_attention_schema`.
MAX_BLIND_SPOTS = 12

#: Hard cap on the characters of a single ``blind_spots`` phrase, so the count
#: bound (:data:`MAX_BLIND_SPOTS`) cannot be defeated by one unbounded entry.
#: Enforced by :func:`build_attention_schema`.
MAX_BLIND_SPOT_CHARS = 120

#: Hard cap on the number of items one moment may admit — the workspace's
#: *limited capacity*, the load-bearing property that makes conscious access a
#: selection rather than a dump. A moment that wants to admit more must exclude
#: and record the loss.
MAX_MOMENT_ITEMS = 12

#: Hard cap on the summed characters of a moment's admitted item summaries, a
#: second bound below :data:`MAX_MOMENT_ITEMS` * :data:`MAX_ITEM_SUMMARY_CHARS`
#: so a moment of maximally long items still cannot dominate a prompt.
MAX_MOMENT_CHARS = 2400

#: Hard cap on the number of :class:`ExcludedCandidate` rows a moment records.
#: The exclusion trace is evidence, not an audit log — a moment that excluded
#: thousands of candidates records only the most-relevant losers up to this cap.
MAX_EXCLUDED_RECORDED = 50

#: Bound on the number of corrupted records a single ledger scan skips before it
#: terminates with a partial result, mirroring
#: :data:`maestro.deed_ledger.MAX_CORRUPT_SKIP` so a corrupted tail can never
#: force a full-file walk.
MAX_CORRUPT_SKIP = 256

#: The append-only episode ledger file, a sibling of the other per-``data_dir``
#: JSONL state (``deed_ledger.jsonl``, ``gate_activations.jsonl``, …).
CONSCIOUS_EPISODES_FILENAME = "conscious_episodes.jsonl"


# ---------------------------------------------------------------------------
# Attention policy — the versioned scoring the producer's deterministic half
# ranks candidates against (proposal 130). Every constant below is a policy
# parameter: changing it changes which facts win a capacity-limited slot, so
# each carries a ``#:`` note stating what it means and what bumping it implies,
# mirroring :mod:`maestro.gate_capacity`'s constant-documentation idiom. The
# repo had no prior policy-version convention; this establishes it.
# ---------------------------------------------------------------------------

#: The attention-policy identity stamped into every
#: :attr:`ConsciousMoment.policy_version`, so a persisted moment names the exact
#: ranking that produced it. Bump this whenever a weight, table, or selection
#: rule below changes in a way that alters which candidates a moment admits: a
#: moment carrying an older version was ranked by a different policy and its
#: hashes are not comparable to a current one's. Read back — not merely written —
#: by :func:`assemble_moment`, which stamps it onto the moment it builds.
ATTENTION_POLICY_VERSION = "attention-policy-1"

#: Weight of the *urgency* term — how much a candidate's trust-derived urgency
#: (:data:`_TRUST_URGENCY`) contributes to its score. Bumping it makes
#: operator/mechanical provenance outrank novelty and recency.
_WEIGHT_URGENCY = 0.30
#: Weight of the *novelty* term — how much a candidate unseen in the predecessor
#: moment outscores a repeat. Bumping it favors churn over a stable focus.
_WEIGHT_NOVELTY = 0.20
#: Weight of the *confidence* term — how much the observing owner's own
#: ``confidence`` in the fact contributes. Bumping it trusts the source's
#: self-assessment more.
_WEIGHT_CONFIDENCE = 0.20
#: Weight of the *actionability* term — how much a candidate's source-kind
#: actionability (:data:`_SOURCE_ACTIONABILITY`) contributes. Bumping it favors
#: facts the loop can act on over facts it can only note.
_WEIGHT_ACTIONABILITY = 0.20
#: Weight of the *age* term — how much recency contributes. Bumping it makes the
#: workspace forget faster. The four weights above plus this one sum to ``1.0``
#: so a raw score stays in ``[0, 1]`` when every term does.
_WEIGHT_AGE = 0.10

#: The novelty term's value for a candidate whose ``(source_kind, source_ref)``
#: identity *was* present in the predecessor moment's admitted set — a repeat
#: still scores, but below a first-seen fact (which scores ``1.0``). Bumping it
#: toward ``1.0`` erases the novelty signal.
_NOVELTY_WHEN_REPEATED = 0.25

#: Half-life, in days, of the exponential age decay: a candidate observed this
#: many days before a moment's ``assembled_at`` contributes half the age term of
#: one observed at that instant. Bumping it lengthens the workspace's memory.
_AGE_HALF_LIFE_DAYS = 3.0

#: Trust class → the *urgency* baseline a candidate of that provenance carries,
#: and the axis :data:`_ADMISSION_TRUST_FLOOR` thresholds against. Derived from
#: *which owner produced the fact*, never from what it says — text claiming
#: operator authority cannot acquire ``authenticated_human`` urgency. Exhaustive
#: over :data:`TrustClass` and indexed directly (not ``.get``) so a new class is
#: a required, visible edit here rather than a silent default.
_TRUST_URGENCY: dict[TrustClass, float] = {
    "authenticated_human": 1.0,
    "mechanical_observation": 0.6,
    "agent_derived": 0.4,
    "untrusted_external": 0.1,
}

#: Source kind → the *actionability* baseline a candidate from that owner
#: carries. Exhaustive over :data:`SourceKind` and indexed directly so a new
#: owner is a required, visible edit here rather than a silent default.
_SOURCE_ACTIONABILITY: dict[SourceKind, float] = {
    "human_directive": 1.0,
    "pr_focus": 0.9,
    "audit_finding": 0.8,
    "diagnostic_snapshot": 0.7,
    "queue_pressure": 0.5,
    "task_outcome": 0.5,
    "prediction_verdict": 0.45,
    "self_model": 0.4,
    "journal_thought": 0.3,
    "budget_state": 0.2,
}

#: The minimum :data:`_TRUST_URGENCY` a candidate's trust class must carry to be
#: admitted at all. Set between ``untrusted_external`` (0.1) and ``agent_derived``
#: (0.4) so untrusted-external provenance alone is dropped as ``untrusted`` before
#: any scoring competition — attention is not authorization, and unauthenticated
#: text never wins a slot however urgent it sounds.
_ADMISSION_TRUST_FLOOR = 0.3


# ---------------------------------------------------------------------------
# The bounded, frozen record shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceItem:
    """One fact admitted to the workspace, with its provenance and bound summary.

    ``item_id`` is an opaque, stable handle a later broadcast/receipt keys on;
    ``source_ref`` is an opaque reference back into the owning observation source
    (a task id, plan id, journal entry id, or finding key). ``summary`` is the
    only free-text field and is always built through :func:`build_workspace_item`
    so it is head-truncated and credential-refused. ``lineage`` names the
    originating workspace item ids a *successor* candidate descends from — the
    recurrence link a verified action outcome carries back into a later moment
    (proposal 130); empty for an ordinary, non-recurrent candidate, and only a
    *mechanical* observation adapter may ever populate it (never self-authored
    narration). Frozen and tuple-only so an item stays hashable and genuinely
    immutable.
    """

    item_id: str
    source_kind: SourceKind
    source_ref: str
    observed_at: str
    trust: TrustClass
    confidence: float
    expires_at: str | None
    summary: str
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExcludedCandidate:
    """One candidate that was considered but not admitted, with why it lost.

    Carries just enough to make an omission legible — the losing item's id and
    source, the ``score`` it was ranked by, and the closed-vocabulary ``reason``.
    Never carries the excluded content itself.
    """

    item_id: str
    source_kind: SourceKind
    score: float
    reason: ExclusionReason


@dataclass(frozen=True)
class AttentionSchema:
    """The system's representation of *its own focus and limits* for one moment.

    ``focus`` is a bounded description of what the moment is attending to;
    ``uncertainty`` its self-assessed confidence gap; ``capacity_used`` /
    ``omitted_count`` the load and what it left out; ``permission_summary`` the
    access profile in force; ``blind_spots`` the measurement gaps it knows it
    cannot see. This is the metacognitive-self-access indicator's data shape — a
    model of focus, never a claim of experience. The free-text fields
    (``focus`` / ``permission_summary`` / ``blind_spots``) are bounded by
    :func:`build_attention_schema`, the sole way to build a schema that respects
    the context-bloat caps — this record is the one most destined for prompt
    broadcast, so it is the one that most needs the cap.
    """

    focus: str
    uncertainty: float
    capacity_used: int
    omitted_count: int
    permission_summary: str
    blind_spots: tuple[str, ...]


@dataclass(frozen=True)
class ConsciousMoment:
    """The capacity-limited record of what is present to the loop at one instant.

    Links to its predecessor via ``predecessor_id`` + ``continuity``, carries the
    admitted ``items`` and the ``excluded`` trace, the ``schema`` modelling its
    own focus, and the two identity hashes (:func:`compute_candidate_hash` over
    the full considered set, :func:`compute_selection_hash` over the admitted set
    + schema). ``max_items`` / ``max_chars`` record the bounds the moment was
    assembled under. Every collection is a ``tuple`` so the moment is hashable and
    immutable. :func:`build_conscious_moment` is the sole constructor that
    *enforces* those bounds (and computes the two hashes); the raw dataclass stays
    unvalidated so the tolerant parser can reconstruct any persisted row.
    """

    moment_id: str
    predecessor_id: str | None
    continuity: ContinuityKind
    assembled_at: str
    policy_version: str
    items: tuple[WorkspaceItem, ...]
    excluded: tuple[ExcludedCandidate, ...]
    schema: AttentionSchema
    candidate_hash: str
    selection_hash: str
    max_items: int
    max_chars: int


@dataclass(frozen=True)
class BroadcastReceipt:
    """Proof that one consumer received a moment (populated by a later increment).

    ``moment_hash`` is the moment's :func:`compute_selection_hash` at delivery, so
    a receipt can be checked against the broadcast identity; ``redacted_item_ids``
    names the items withheld from *this* consumer. Lands now, unpopulated, so the
    on-disk shape does not churn when broadcast arrives.
    """

    moment_id: str
    moment_hash: str
    consumer: str
    delivered_at: str
    redacted_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ConsciousEpisode:
    """One persisted episode — a moment, its receipts, and its decision citations.

    ``cited_task_ids`` / ``cited_plan_ids`` are the decision-provenance fields
    naming the tasks and plans that cited this moment (proposal 130):
    ``cited_plan_ids`` is populated by ``evolve``'s plan-proposal seam and
    ``cited_task_ids`` by ``TaskGenerator.generate_task``, both drained into the
    finalized row by ``MaestroAgent._finalize_conscious_episode`` (either is empty
    when no decision of that kind cited the moment). The append-only unit of
    :class:`ConsciousEpisodeBackend`.
    """

    moment: ConsciousMoment
    receipts: tuple[BroadcastReceipt, ...]
    cited_task_ids: tuple[str, ...]
    cited_plan_ids: tuple[str, ...]
    recorded_at: str


# ---------------------------------------------------------------------------
# Construction — the sole item constructor, with the credential refusal
# ---------------------------------------------------------------------------


def _truncate_summary(s: str, limit: int) -> str:
    """Head-truncate *s* to at most *limit* chars with a trailing ellipsis.

    Returns *s* unchanged when ``len(s) <= limit``; otherwise ``s[:limit - 1] +
    "…"``, exactly *limit* chars. The head-keep shape of
    :meth:`maestro.consciousness.Consciousness._truncate_item`, inlined here so
    the leaf owner keeps its no-``consciousness``-import boundary.
    """
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def build_workspace_item(
    *,
    item_id: str,
    source_kind: SourceKind,
    source_ref: str,
    observed_at: str,
    trust: TrustClass,
    confidence: float,
    expires_at: str | None,
    summary: str,
    lineage: Sequence[str] = (),
) -> WorkspaceItem:
    """Construct a :class:`WorkspaceItem` — the *sole* way to make one.

    Refuses, by raising :class:`ValueError`, any *summary* that trips
    :func:`maestro.secret_scan.has_hard_block` — checked against the **original**
    summary before truncation, so a credential in a summary's tail cannot be
    truncated into safety and then persisted. Summaries are *not* routed through
    :func:`maestro.secret_scan.scan_contents`: its narrative redactions are
    publication-specific by that function's own docstring. On the accept path the
    summary is head-truncated to :data:`MAX_ITEM_SUMMARY_CHARS`.

    *lineage* names the originating item ids a successor candidate descends from
    (proposal 130) and is stored as a ``tuple``; it defaults to empty, so an
    ordinary candidate is byte-identical to before.
    """
    if has_hard_block(summary):
        raise ValueError(
            "refusing to build a WorkspaceItem: summary contains a hard-block "
            "credential and must never be persisted to the workspace ledger",
        )
    return WorkspaceItem(
        item_id=item_id,
        source_kind=source_kind,
        source_ref=source_ref,
        observed_at=observed_at,
        trust=trust,
        confidence=confidence,
        expires_at=expires_at,
        summary=_truncate_summary(summary, MAX_ITEM_SUMMARY_CHARS),
        lineage=tuple(lineage),
    )


def build_attention_schema(
    *,
    focus: str,
    uncertainty: float,
    capacity_used: int,
    omitted_count: int,
    permission_summary: str,
    blind_spots: Sequence[str],
) -> AttentionSchema:
    """Construct an :class:`AttentionSchema` — the *sole* bounding constructor.

    Head-truncates ``focus`` to :data:`MAX_FOCUS_CHARS` and
    ``permission_summary`` to :data:`MAX_PERMISSION_SUMMARY_CHARS`, and keeps at
    most :data:`MAX_BLIND_SPOTS` blind-spot phrases, each truncated to
    :data:`MAX_BLIND_SPOT_CHARS`. The schema rides inside
    :func:`compute_selection_hash` toward prompt broadcast, so its free-text
    fields carry the same context-bloat discipline as an item summary. The raw
    dataclass stays unvalidated so the tolerant parser can reconstruct any
    persisted row.
    """
    return AttentionSchema(
        focus=_truncate_summary(focus, MAX_FOCUS_CHARS),
        uncertainty=uncertainty,
        capacity_used=capacity_used,
        omitted_count=omitted_count,
        permission_summary=_truncate_summary(
            permission_summary, MAX_PERMISSION_SUMMARY_CHARS,
        ),
        blind_spots=tuple(
            _truncate_summary(spot, MAX_BLIND_SPOT_CHARS)
            for spot in blind_spots[:MAX_BLIND_SPOTS]
        ),
    )


# ---------------------------------------------------------------------------
# Moment identity hashes
# ---------------------------------------------------------------------------


def compute_candidate_hash(
    items: Sequence[WorkspaceItem],
    excluded: Sequence[ExcludedCandidate],
) -> str:
    """Return the canonical SHA-256 hash over *every candidate considered*.

    Covers the full considered set — the admitted *items* **and** the *excluded*
    candidates — so moving one candidate from admitted to excluded (an
    admitted/excluded swap) changes the hash even though the set of ids is
    unchanged. Canonicalises with ``sort_keys`` + compact separators before
    hashing, mirroring
    :func:`maestro.memory.compute_plan_content_hash`, so the digest is
    order-insensitive to dict keys but sensitive to composition.
    """
    canonical = json.dumps(
        {
            "items": [_item_dict(i) for i in items],
            "excluded": [_excluded_dict(c) for c in excluded],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_selection_hash(
    items: Sequence[WorkspaceItem],
    schema: AttentionSchema,
) -> str:
    """Return the canonical SHA-256 hash over the admitted set plus the schema.

    This is the moment's **broadcast identity**: the value a later increment
    compares across consumers to prove they all received the same admitted set
    and attention schema. Any change to an admitted item, or to the schema,
    changes it. Canonicalises identically to :func:`compute_candidate_hash` and
    :func:`maestro.memory.compute_plan_content_hash`. It is deliberately *not*
    re-derived anywhere else — every site that needs the broadcast identity must
    call this function.
    """
    canonical = json.dumps(
        {
            "items": [_item_dict(i) for i in items],
            "schema": _schema_dict(schema),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Moment construction — the sole cap-enforcing moment constructor
# ---------------------------------------------------------------------------


def build_conscious_moment(
    *,
    moment_id: str,
    predecessor_id: str | None,
    continuity: ContinuityKind,
    assembled_at: str,
    policy_version: str,
    items: Sequence[WorkspaceItem],
    excluded: Sequence[ExcludedCandidate],
    schema: AttentionSchema,
    max_items: int = MAX_MOMENT_ITEMS,
    max_chars: int = MAX_MOMENT_CHARS,
) -> ConsciousMoment:
    """Construct a :class:`ConsciousMoment`, enforcing the capacity bounds.

    The *sole* way to assemble a moment that respects the workspace's limited
    capacity — the load-bearing property that makes conscious access a selection
    rather than a dump. Raises :class:`ValueError` when the admitted *items*
    exceed *max_items*, when their summaries sum above *max_chars*, or when the
    *excluded* trace exceeds :data:`MAX_EXCLUDED_RECORDED`. *max_items* /
    *max_chars* default to :data:`MAX_MOMENT_ITEMS` / :data:`MAX_MOMENT_CHARS` and
    are recorded on the moment as the bounds it was assembled under. The two
    identity hashes are derived here via :func:`compute_candidate_hash` /
    :func:`compute_selection_hash` (never re-derived), so a caller cannot stamp a
    moment with a hash that disagrees with its content. The raw dataclass stays
    unvalidated so the tolerant parser can reconstruct any persisted row.
    """
    if len(items) > max_items:
        raise ValueError(
            f"refusing to build a ConsciousMoment: {len(items)} items exceed "
            f"max_items={max_items}",
        )
    total_chars = sum(len(item.summary) for item in items)
    if total_chars > max_chars:
        raise ValueError(
            f"refusing to build a ConsciousMoment: item summaries sum to "
            f"{total_chars} chars, over max_chars={max_chars}",
        )
    if len(excluded) > MAX_EXCLUDED_RECORDED:
        raise ValueError(
            f"refusing to build a ConsciousMoment: {len(excluded)} excluded "
            f"candidates exceed MAX_EXCLUDED_RECORDED={MAX_EXCLUDED_RECORDED}",
        )
    return ConsciousMoment(
        moment_id=moment_id,
        predecessor_id=predecessor_id,
        continuity=continuity,
        assembled_at=assembled_at,
        policy_version=policy_version,
        items=tuple(items),
        excluded=tuple(excluded),
        schema=schema,
        candidate_hash=compute_candidate_hash(items, excluded),
        selection_hash=compute_selection_hash(items, schema),
        max_items=max_items,
        max_chars=max_chars,
    )


# ---------------------------------------------------------------------------
# The pure candidate scorer (proposal 130)
# ---------------------------------------------------------------------------


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning ``None`` on any malformed input.

    A deliberate sentinel-return classification, not a silenced handler: a
    candidate whose timestamp cannot be parsed gets no recency credit rather
    than crashing the pure scorer: an unparseable timestamp is treated as
    unknown age, mirroring :func:`maestro.diagnostics._to_int`.
    """
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _age_days(observed_at: str, now: str) -> float | None:
    """Return the non-negative age in days of *observed_at* relative to *now*.

    Both are ISO-8601 strings. Returns ``None`` when either cannot be parsed or
    the two are not comparable (a naive/aware mix). Clamps a future
    *observed_at* to age zero — clock skew on this long-running loop must never
    sign-flip recency.
    """
    observed = _parse_ts(observed_at)
    reference = _parse_ts(now)
    if observed is None or reference is None:
        return None
    try:
        seconds = (reference - observed).total_seconds()
    except TypeError:
        return None
    return max(0.0, seconds) / 86400.0


def score_candidate(
    item: WorkspaceItem,
    *,
    now: str,
    predecessor_source_refs: frozenset[tuple[str, str]],
) -> float:
    """Return *item*'s attention score under :data:`ATTENTION_POLICY_VERSION`.

    A pure function of the injected record and clock: the five weighted terms
    (:data:`_WEIGHT_URGENCY` … :data:`_WEIGHT_AGE`) summed. Urgency and
    actionability come from the provenance tables (:data:`_TRUST_URGENCY` /
    :data:`_SOURCE_ACTIONABILITY`) — trust is derived from *which owner produced
    the fact*, never from its text. Novelty is ``1.0`` when the item's
    ``(source_kind, source_ref)`` identity is absent from *predecessor_source_refs*
    (the predecessor moment's admitted identities) and
    :data:`_NOVELTY_WHEN_REPEATED` otherwise — from source identity, not text
    overlap, so the leaf needs no tokenizer. Confidence is the owner's own
    ``item.confidence``. Age is an exponential decay of ``observed_at`` versus
    *now* with a :data:`_AGE_HALF_LIFE_DAYS`-day half-life; an undateable item
    gets no recency credit.
    """
    urgency = _TRUST_URGENCY[item.trust]
    actionability = _SOURCE_ACTIONABILITY[item.source_kind]
    seen = (item.source_kind, item.source_ref) in predecessor_source_refs
    novelty = _NOVELTY_WHEN_REPEATED if seen else 1.0
    age_days = _age_days(item.observed_at, now)
    age_term = 0.0 if age_days is None else 0.5 ** (age_days / _AGE_HALF_LIFE_DAYS)
    return (
        _WEIGHT_URGENCY * urgency
        + _WEIGHT_NOVELTY * novelty
        + _WEIGHT_CONFIDENCE * item.confidence
        + _WEIGHT_ACTIONABILITY * actionability
        + _WEIGHT_AGE * age_term
    )


# ---------------------------------------------------------------------------
# The deterministic selection policy (proposal 130)
# ---------------------------------------------------------------------------


def _is_expired(item: WorkspaceItem, now: str) -> bool:
    """Return whether *item*'s ``expires_at`` has passed at *now*.

    A ``None`` expiry never expires. An unparseable ``expires_at`` or *now*, or a
    naive/aware mismatch, is treated as *not* expired — the policy never drops a
    candidate it cannot prove stale (fail-open on a bad timestamp, the
    conservative choice for admission).
    """
    if item.expires_at is None:
        return False
    expiry = _parse_ts(item.expires_at)
    reference = _parse_ts(now)
    if expiry is None or reference is None:
        return False
    try:
        return expiry <= reference
    except TypeError:
        return False


def select_workspace_items(
    candidates: Sequence[WorkspaceItem],
    *,
    now: str,
    predecessor_source_refs: frozenset[tuple[str, str]],
    max_items: int = MAX_MOMENT_ITEMS,
    max_chars: int = MAX_MOMENT_CHARS,
) -> tuple[list[WorkspaceItem], list[ExcludedCandidate]]:
    """Select the admitted set from *candidates*, recording why each loser lost.

    A pure, deterministic function: identical inputs yield an identical admitted
    list and an identical ordered exclusion trace. The rules run in this fixed
    order, each stamping its closed-vocabulary :class:`ExclusionReason`:

    * **expired** — a candidate whose ``expires_at`` has passed at *now*.
    * **untrusted** — a candidate whose trust class scores below
      :data:`_ADMISSION_TRUST_FLOOR` on :data:`_TRUST_URGENCY` (only
      ``untrusted_external`` under the shipped table). Attention is not
      authorization: unauthenticated provenance is dropped before it can compete.
    * **duplicate_source** — of several survivors sharing one
      ``(source_kind, source_ref)`` identity, only the highest scorer is kept,
      ties broken by first position; the rest are collapsed.
    * The highest-scoring ``authenticated_human`` survivor is then *reserved* a
      slot so an operator directive is admitted even when it would lose on score.
    * The remaining survivors are ranked by score descending, ``item_id``
      ascending, and admitted until a capacity bound is reached:
      **capacity_exceeded** once the admitted set is at *max_items*, or
      **low_score** once the summed-``summary`` *max_chars* budget can no longer
      fit the next ranked item while item slots remain.

    Returns ``(admitted, excluded)``. *admitted* is ordered by score descending
    (``item_id`` tie-break) so the moment's ``items`` tuple — and thus its two
    hashes — is canonical. *excluded* keeps the highest-scoring losers up to
    :data:`MAX_EXCLUDED_RECORDED`, so :func:`build_conscious_moment` never raises
    on trace length; the *full* loser count is recovered by the caller as
    ``len(candidates) - len(admitted)``, never from this clipped trace. Mirrors
    the ordered-rule docstring of :func:`maestro.outcome_authority.decide_outcome`.
    """
    excluded: list[ExcludedCandidate] = []

    def _drop(item: WorkspaceItem, score: float, reason: ExclusionReason) -> None:
        excluded.append(
            ExcludedCandidate(
                item_id=item.item_id,
                source_kind=item.source_kind,
                score=score,
                reason=reason,
            ),
        )

    # Score every candidate once, up front, so ranking and exclusion agree.
    scored: list[tuple[WorkspaceItem, float]] = [
        (
            item,
            score_candidate(
                item, now=now, predecessor_source_refs=predecessor_source_refs,
            ),
        )
        for item in candidates
    ]

    # Rule 1 — expired.
    live: list[tuple[WorkspaceItem, float]] = []
    for item, score in scored:
        if _is_expired(item, now):
            _drop(item, score, "expired")
        else:
            live.append((item, score))

    # Rule 2 — trust floor: unauthenticated provenance never competes.
    trusted: list[tuple[WorkspaceItem, float]] = []
    for item, score in live:
        if _TRUST_URGENCY[item.trust] < _ADMISSION_TRUST_FLOOR:
            _drop(item, score, "untrusted")
        else:
            trusted.append((item, score))

    # Rule 3 — collapse duplicate (source_kind, source_ref) identities, keeping
    # the highest scorer; strict ``>`` keeps the earliest position on a tie.
    winner_index: dict[tuple[str, str], int] = {}
    for idx, (item, score) in enumerate(trusted):
        key = (item.source_kind, item.source_ref)
        incumbent = winner_index.get(key)
        if incumbent is None or score > trusted[incumbent][1]:
            winner_index[key] = idx
    winners = set(winner_index.values())
    deduped: list[tuple[WorkspaceItem, float]] = []
    for idx, (item, score) in enumerate(trusted):
        if idx in winners:
            deduped.append((item, score))
        else:
            _drop(item, score, "duplicate_source")

    # Rule 4 — reserve the highest-scoring authenticated-human survivor a slot.
    reserved_idx: int | None = None
    if max_items >= 1:
        human_indices = [
            i for i, (item, _score) in enumerate(deduped)
            if item.trust == "authenticated_human"
        ]
        if human_indices:
            reserved_idx = min(
                human_indices,
                key=lambda i: (-deduped[i][1], deduped[i][0].item_id),
            )

    # Rule 5 — rank the rest, admit under both caps.
    contenders = [pair for i, pair in enumerate(deduped) if i != reserved_idx]
    contenders.sort(key=lambda pair: (-pair[1], pair[0].item_id))

    admitted_pairs: list[tuple[WorkspaceItem, float]] = []
    used_chars = 0
    if reserved_idx is not None:
        reserved = deduped[reserved_idx]
        admitted_pairs.append(reserved)
        used_chars += len(reserved[0].summary)

    cut = len(contenders)
    for i, (item, _score) in enumerate(contenders):
        if len(admitted_pairs) >= max_items or used_chars + len(item.summary) > max_chars:
            cut = i
            break
        admitted_pairs.append((item, _score))
        used_chars += len(item.summary)
    if cut < len(contenders):
        # Label the un-admitted tail by the cap that stopped admission: the item
        # cap ("already at max_items") is ``capacity_exceeded``; the char budget,
        # with item slots still free, leaves the lower-ranked tail as ``low_score``.
        reason: ExclusionReason = (
            "capacity_exceeded" if len(admitted_pairs) >= max_items else "low_score"
        )
        for item, score in contenders[cut:]:
            _drop(item, score, reason)

    admitted_pairs.sort(key=lambda pair: (-pair[1], pair[0].item_id))
    admitted = [item for item, _score in admitted_pairs]

    # Keep the highest-scoring losers so the trace never exceeds the cap.
    excluded.sort(key=lambda ex: (-ex.score, ex.item_id))
    return admitted, excluded[:MAX_EXCLUDED_RECORDED]


# ---------------------------------------------------------------------------
# The moment assembler (proposal 130)
# ---------------------------------------------------------------------------


def derive_moment_id(
    *,
    predecessor_id: str | None,
    assembled_at: str,
    policy_version: str,
    candidate_ids: Sequence[str],
) -> str:
    """Return a deterministic ``cm-<hash8>`` id for a moment.

    Derived from a canonical digest over the predecessor, assembly time, policy
    version, and the ordered candidate identities, so identical inputs yield an
    identical id and any change to the considered set (membership or order)
    changes it. Reuses the short-``sha256`` fingerprint shape
    :mod:`maestro.evolution` uses for overflow findings (there ``of-<hash8>``);
    the ``cm-`` prefix marks a conscious moment.
    """
    canonical = json.dumps(
        {
            "predecessor_id": predecessor_id,
            "assembled_at": assembled_at,
            "policy_version": policy_version,
            "candidate_ids": list(candidate_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"cm-{digest}"


def assemble_moment(
    candidates: Sequence[WorkspaceItem],
    *,
    assembled_at: str,
    permission_summary: str,
    blind_spots: Sequence[str],
    predecessor_id: str | None,
    continuity: ContinuityKind,
    predecessor_source_refs: frozenset[tuple[str, str]] = frozenset(),
    max_items: int = MAX_MOMENT_ITEMS,
    max_chars: int = MAX_MOMENT_CHARS,
    policy_version: str = ATTENTION_POLICY_VERSION,
) -> ConsciousMoment:
    """Assemble a :class:`ConsciousMoment` from already-observed *candidates*.

    The pure composition
    :func:`select_workspace_items` → :func:`build_attention_schema` →
    :func:`build_conscious_moment`. Takes *assembled_at* as the scoring clock and
    every other input as a parameter — it reads no clock and no state, so
    identical inputs yield an identical moment: the same ``moment_id``, the same
    two hashes, and the same ordered exclusion trace.

    The schema's ``capacity_used`` is the admitted count and ``omitted_count`` is
    *every* candidate that lost — ``len(candidates) - len(admitted)``, not merely
    the clipped exclusion trace. ``focus`` is the top admitted item's summary (or
    empty when nothing is admitted); ``uncertainty`` is ``1 - mean confidence``
    over the admitted set (maximal when the set is empty), the confidence gap the
    :class:`AttentionSchema` field names.
    """
    admitted, excluded = select_workspace_items(
        candidates,
        now=assembled_at,
        predecessor_source_refs=predecessor_source_refs,
        max_items=max_items,
        max_chars=max_chars,
    )
    if admitted:
        mean_confidence = sum(item.confidence for item in admitted) / len(admitted)
        focus = admitted[0].summary
    else:
        mean_confidence = 0.0
        focus = ""
    uncertainty = max(0.0, min(1.0, 1.0 - mean_confidence))
    schema = build_attention_schema(
        focus=focus,
        uncertainty=uncertainty,
        capacity_used=len(admitted),
        omitted_count=len(candidates) - len(admitted),
        permission_summary=permission_summary,
        blind_spots=blind_spots,
    )
    moment_id = derive_moment_id(
        predecessor_id=predecessor_id,
        assembled_at=assembled_at,
        policy_version=policy_version,
        candidate_ids=[item.item_id for item in candidates],
    )
    return build_conscious_moment(
        moment_id=moment_id,
        predecessor_id=predecessor_id,
        continuity=continuity,
        assembled_at=assembled_at,
        policy_version=policy_version,
        items=admitted,
        excluded=excluded,
        schema=schema,
        max_items=max_items,
        max_chars=max_chars,
    )


# ---------------------------------------------------------------------------
# The broadcast owner — render one selected moment to a consumer, prove delivery
# (proposal 130). Advisory only: the rendered block is prompt text, grants no
# capability, approves no plan, and consults no gate. Attention is not
# authorization.
# ---------------------------------------------------------------------------


def _redact_for_consumer(
    moment: ConsciousMoment, consumer: RenderConsumer,
) -> tuple[tuple[WorkspaceItem, ...], tuple[str, ...]]:
    """Return the admitted items *consumer* may see and the ids withheld from it.

    Defense-in-depth over the admission trust floor: an admitted item whose trust
    class scores below :data:`_ADMISSION_TRUST_FLOOR` on :data:`_TRUST_URGENCY`
    (only ``untrusted_external`` under the shipped table) is withheld from every
    reasoning consumer — attention is never authorization, so untrusted-external
    provenance never reaches a consumer's prompt even if a future admission path
    let one into the admitted set. Every consumer shares this one policy in this
    increment; the *consumer* parameter is the seam a later increment threads a
    per-consumer policy through. Returns ``(visible, withheld_ids)``, preserving
    the moment's canonical admitted order so the rendered block is deterministic.
    """
    visible: list[WorkspaceItem] = []
    withheld: list[str] = []
    for item in moment.items:
        if _TRUST_URGENCY[item.trust] < _ADMISSION_TRUST_FLOOR:
            withheld.append(item.item_id)
        else:
            visible.append(item)
    return tuple(visible), tuple(withheld)


def render_active_moment(
    moment: ConsciousMoment, consumer: RenderConsumer,
) -> tuple[str, tuple[str, ...]]:
    """Render *moment* to a bounded, labeled block for *consumer*.

    The **sole** formatter turning a :class:`ConsciousMoment` into the prompt text
    a reasoning consumer receives. Returns ``(block, redacted_item_ids)``: the
    block carries only the items :func:`_redact_for_consumer` admits for this
    consumer (each with its item_id/source/trust/summary — the id is the stable
    handle a consumer names to cite the exact item it used), plus the schema's focus,
    uncertainty, capacity-used/omitted counts, permission summary, and blind
    spots, and closes with the moment id, ``selection_hash``, and ``assembled_at``;
    *redacted_item_ids* names the admitted items withheld from this consumer, so
    the paired :class:`BroadcastReceipt` can prove what it did not see. The block
    is inherently bounded — every field it renders already carries a hard content
    cap from moment assembly, so it adds no new cap of its own. Pure and
    import-safe: reads only the injected record.
    """
    visible, redacted_item_ids = _redact_for_consumer(moment, consumer)
    schema = moment.schema
    lines: list[str] = [
        f"## Conscious moment (broadcast to {consumer})",
        f"Focus: {schema.focus or '(nothing admitted)'}",
        f"Uncertainty: {schema.uncertainty:.2f}",
        f"Capacity: {schema.capacity_used} admitted, {schema.omitted_count} omitted",
        f"Permission: {schema.permission_summary}",
    ]
    if schema.blind_spots:
        lines.append("Blind spots: " + "; ".join(schema.blind_spots))
    if visible:
        lines.append("Admitted items:")
        lines.extend(
            f"- {item.item_id} [{item.source_kind}/{item.trust}] {item.summary}"
            for item in visible
        )
    else:
        lines.append("Admitted items: (none visible to this consumer)")
    if redacted_item_ids:
        lines.append(
            f"Withheld from this consumer: {len(redacted_item_ids)} item(s)",
        )
    lines.append(
        f"Moment {moment.moment_id} · selection_hash {moment.selection_hash} · "
        f"assembled_at {moment.assembled_at}",
    )
    return "\n".join(lines), redacted_item_ids


def build_access_profile(
    episode: ConsciousEpisode, consumer: RenderConsumer,
) -> dict[str, Any]:
    """Build the JSON-ready, counts-only access profile for *episode*'s moment.

    The structural companion to :func:`render_active_moment`: where the renderer
    emits the consumer-visible admitted *content*, this emits a pure view of the
    moment's identity, its attention schema, how loaded and how redacted the
    moment is for *consumer*, and how widely the episode was shared and cited —
    **never** an item summary, an item id, or an excluded id, so an omission is
    surfaced only as a count keyed by its closed-vocabulary reason.

    ``visible_item_count`` / ``withheld_from_consumer`` come from the single
    :func:`_redact_for_consumer` pass (never a second copy of the redaction), so
    the profile can never disagree with the block the renderer produced.
    ``excluded_counts`` is keyed by *every* :data:`ExclusionReason` member,
    zero-filled and sorted, so its shape is stable no matter which reasons the
    trace happens to carry. Pure: reads only the injected *episode*, touches no
    state, calls no gate.
    """
    moment = episode.moment
    schema = moment.schema
    visible, withheld = _redact_for_consumer(moment, consumer)
    excluded_counts: dict[str, int] = {r: 0 for r in sorted(_EXCLUSION_REASONS)}
    for candidate in moment.excluded:
        excluded_counts[candidate.reason] += 1
    return {
        "moment_id": moment.moment_id,
        "predecessor_id": moment.predecessor_id,
        "continuity": moment.continuity,
        "assembled_at": moment.assembled_at,
        "policy_version": moment.policy_version,
        "candidate_hash": moment.candidate_hash,
        "selection_hash": moment.selection_hash,
        "max_items": moment.max_items,
        "max_chars": moment.max_chars,
        "focus": schema.focus,
        "uncertainty": schema.uncertainty,
        "capacity_used": schema.capacity_used,
        "omitted_count": schema.omitted_count,
        "permission_summary": schema.permission_summary,
        "blind_spots": list(schema.blind_spots),
        "consumer": consumer,
        "visible_item_count": len(visible),
        "withheld_from_consumer": len(withheld),
        "excluded_recorded": len(moment.excluded),
        "excluded_counts": excluded_counts,
        "receipt_count": len(episode.receipts),
        "cited_task_count": len(episode.cited_task_ids),
        "cited_plan_count": len(episode.cited_plan_ids),
        "episode_recorded_at": episode.recorded_at,
    }


def build_broadcast_receipt(
    *,
    moment: ConsciousMoment,
    consumer: ConsumerName,
    delivered_at: str,
    redacted_item_ids: Sequence[str],
) -> BroadcastReceipt:
    """Construct a :class:`BroadcastReceipt` proving *moment* reached *consumer*.

    Stamps ``moment_hash = moment.selection_hash`` (the broadcast identity from
    :func:`compute_selection_hash`, stamped once at moment construction and never
    re-derived), so a receipt can be checked against the exact admitted set +
    schema the consumer received. *redacted_item_ids* names the items
    :func:`_redact_for_consumer` withheld from this consumer. The sole constructor
    of a populated receipt.
    """
    return BroadcastReceipt(
        moment_id=moment.moment_id,
        moment_hash=moment.selection_hash,
        consumer=consumer,
        delivered_at=delivered_at,
        redacted_item_ids=tuple(redacted_item_ids),
    )


# ---------------------------------------------------------------------------
# The fail-closed citation validator — the single authority deciding whether a
# stamped decision citation actually belongs to the moment it names (proposal
# 130). Pure, no I/O, no gate: a leaf-module function like its neighbours. The
# producers already *silently* drop a non-visible id before stamping; this is the
# independent stop condition that makes a cross-moment, unresolved, or otherwise
# tampered citation *visible* at the commit seam so the guard can null it. The
# plan or task itself is never dropped here — citations are strictly advisory.
# ---------------------------------------------------------------------------


#: Why a stamped decision citation failed validation against the live moment.
#: ``orphan_moment`` — item ids were cited with no moment id to anchor them;
#: ``unresolved_moment`` — a moment id was cited but no live moment resolves it;
#: ``cross_moment`` — the cited moment id names a different moment than the live
#: one; ``unknown_item`` — a cited item id is not in the consumer-visible admitted
#: set of the named moment. Kept a ``Literal`` so a new reason is a one-line
#: vocabulary edit, mirroring :data:`ExclusionReason`.
CitationRejectReason = Literal[
    "orphan_moment",
    "cross_moment",
    "unresolved_moment",
    "unknown_item",
]


@dataclass(frozen=True)
class CitationVerdict:
    """The outcome of validating a stamped citation against the live moment.

    ``valid`` is ``True`` when the citation belongs to the moment it names.
    ``reason`` is ``None`` on a valid verdict and otherwise the closed-vocabulary
    slug naming *why* it failed — the frozen-verdict-plus-optional-reason shape of
    :class:`maestro.plan_grounding.GroundingVerdict`. ``consumer`` echoes the
    broadcast consumer the citation was validated for, so a caller (or a test) can
    see which consumer's visibility rule ran without re-deriving it.
    """

    valid: bool
    reason: CitationRejectReason | None
    consumer: ConsumerName


def visible_item_ids(
    moment: ConsciousMoment, consumer: ConsumerName,
) -> frozenset[str]:
    """Return the ids of the admitted items *consumer* may see in *moment*.

    A thin public wrapper over :func:`_redact_for_consumer` (the per-consumer
    trust-floor visibility rule — never re-implement the redaction): the item ids
    of the admitted set minus the ids withheld from this consumer. The set the
    delivery seam hands a producer and the set :func:`validate_citation`
    re-derives are one and the same, so a citation can be checked against the
    exact admitted set the consumer actually saw. Pure, import-safe.
    """
    seen, _withheld = _redact_for_consumer(moment, consumer)
    return frozenset(item.item_id for item in seen)


def validate_citation(
    *,
    moment_id: str | None,
    item_ids: list[str] | None,
    moment: ConsciousMoment | None,
    consumer: ConsumerName,
) -> CitationVerdict:
    """Decide whether a stamped ``(moment_id, item_ids)`` citation is legitimate.

    The single authority the plan-submission and task-enqueue guards call before
    committing a citation. Advisory-only by contract: a caller nulls the
    provenance on an invalid verdict but never drops the plan or task. The ordered
    rules, each returning its closed-vocabulary :data:`CitationRejectReason`:

    * A ``None`` *moment_id* is valid only when *item_ids* is also ``None`` — the
      legacy / never-cited row. Item ids present with no moment to anchor them is
      ``orphan_moment``.
    * A set *moment_id* with no live *moment* is ``unresolved_moment``.
    * A live *moment* whose ``moment_id`` differs from the cited one is
      ``cross_moment`` — the citation names a different moment than the one served.
    * Any cited id outside :func:`visible_item_ids` for this *consumer* is
      ``unknown_item`` — an id the consumer never saw (redacted, hallucinated, or
      from another moment).
    * Otherwise valid. An empty *item_ids* under a matching moment ("saw the
      moment, cited nothing") stays valid.

    Pure: reads only the injected records, touches no state, consults no gate.
    """
    if moment_id is None:
        if item_ids is None:
            return CitationVerdict(valid=True, reason=None, consumer=consumer)
        return CitationVerdict(
            valid=False, reason="orphan_moment", consumer=consumer,
        )
    if moment is None:
        return CitationVerdict(
            valid=False, reason="unresolved_moment", consumer=consumer,
        )
    if moment.moment_id != moment_id:
        return CitationVerdict(
            valid=False, reason="cross_moment", consumer=consumer,
        )
    visible = visible_item_ids(moment, consumer)
    for item_id in item_ids or ():
        if item_id not in visible:
            return CitationVerdict(
                valid=False, reason="unknown_item", consumer=consumer,
            )
    return CitationVerdict(valid=True, reason=None, consumer=consumer)


# ---------------------------------------------------------------------------
# Explicit serialize-out (one serializer, shared by both backends)
# ---------------------------------------------------------------------------


def _item_dict(item: WorkspaceItem) -> dict[str, Any]:
    """Serialize a :class:`WorkspaceItem` to a plain JSON-ready dict.

    The ``lineage`` key is emitted **only when non-empty**, so a lineage-less
    item — and therefore both moment hashes, which fold this dict — stays
    byte-identical to a pre-``lineage`` row (proposal 130).
    """
    data: dict[str, Any] = {
        "item_id": item.item_id,
        "source_kind": item.source_kind,
        "source_ref": item.source_ref,
        "observed_at": item.observed_at,
        "trust": item.trust,
        "confidence": item.confidence,
        "expires_at": item.expires_at,
        "summary": item.summary,
    }
    if item.lineage:
        data["lineage"] = list(item.lineage)
    return data


def _excluded_dict(candidate: ExcludedCandidate) -> dict[str, Any]:
    """Serialize an :class:`ExcludedCandidate` to a plain JSON-ready dict."""
    return {
        "item_id": candidate.item_id,
        "source_kind": candidate.source_kind,
        "score": candidate.score,
        "reason": candidate.reason,
    }


def _schema_dict(schema: AttentionSchema) -> dict[str, Any]:
    """Serialize an :class:`AttentionSchema` to a plain JSON-ready dict."""
    return {
        "focus": schema.focus,
        "uncertainty": schema.uncertainty,
        "capacity_used": schema.capacity_used,
        "omitted_count": schema.omitted_count,
        "permission_summary": schema.permission_summary,
        "blind_spots": list(schema.blind_spots),
    }


def _receipt_dict(receipt: BroadcastReceipt) -> dict[str, Any]:
    """Serialize a :class:`BroadcastReceipt` to a plain JSON-ready dict."""
    return {
        "moment_id": receipt.moment_id,
        "moment_hash": receipt.moment_hash,
        "consumer": receipt.consumer,
        "delivered_at": receipt.delivered_at,
        "redacted_item_ids": list(receipt.redacted_item_ids),
    }


def _moment_dict(moment: ConsciousMoment) -> dict[str, Any]:
    """Serialize a :class:`ConsciousMoment` to a plain JSON-ready dict."""
    return {
        "moment_id": moment.moment_id,
        "predecessor_id": moment.predecessor_id,
        "continuity": moment.continuity,
        "assembled_at": moment.assembled_at,
        "policy_version": moment.policy_version,
        "items": [_item_dict(i) for i in moment.items],
        "excluded": [_excluded_dict(c) for c in moment.excluded],
        "schema": _schema_dict(moment.schema),
        "candidate_hash": moment.candidate_hash,
        "selection_hash": moment.selection_hash,
        "max_items": moment.max_items,
        "max_chars": moment.max_chars,
    }


def episode_to_dict(episode: ConsciousEpisode) -> dict[str, Any]:
    """Serialize a :class:`ConsciousEpisode` to a plain JSON-ready dict.

    The single explicit serializer both persistence substrates use (the JSONL
    :func:`_row` and the SQLite ``append``/seed), so the on-disk shape can never
    drift between them. Explicit per-field, not :func:`dataclasses.asdict`,
    because the round-trip in reverse (:func:`_parse_conscious_episode_dict`)
    must reconstruct every ``tuple`` field and nested record by hand — a naive
    ``asdict`` → ``json`` → field-filter round-trip compares unequal, since JSON
    turns every ``tuple`` into a ``list`` and the field-filter shape leaves nested
    members as plain ``dict``s.
    """
    return {
        "moment": _moment_dict(episode.moment),
        "receipts": [_receipt_dict(r) for r in episode.receipts],
        "cited_task_ids": list(episode.cited_task_ids),
        "cited_plan_ids": list(episode.cited_plan_ids),
        "recorded_at": episode.recorded_at,
    }


def _row(episode: ConsciousEpisode) -> str:
    """Serialize one episode to a JSONL row (no trailing newline)."""
    return json.dumps(episode_to_dict(episode))


# ---------------------------------------------------------------------------
# Tolerant coercion helpers (explicit tuple-coerce-in)
# ---------------------------------------------------------------------------


def _opt_str(value: object) -> str | None:
    """Return *value* when it is a ``str``, else ``None`` (absent or malformed)."""
    return value if isinstance(value, str) else None


def _member(value: object, members: frozenset[str]) -> str | None:
    """Return *value* when it is a ``str`` inside *members*, else ``None``."""
    if isinstance(value, str) and value in members:
        return value
    return None


def _as_float(value: object) -> float | None:
    """Return *value* coerced to ``float``, or ``None`` when non-numeric.

    ``bool`` is rejected (it subclasses ``int``) so a JSON ``true`` can never
    masquerade as a confidence/score of ``1.0``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: object) -> int | None:
    """Return *value* when it is a non-``bool`` ``int``, else ``None``."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _str_tuple(value: object) -> tuple[str, ...]:
    """Return the ``str`` members of *value* as a tuple; ``()`` when not a list."""
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))


def _str_tuple_strict(value: object) -> tuple[str, ...] | None:
    """Fail-closed variant for decision-provenance citation lists.

    An *absent* field (``None`` — the unpopulated state, since no writer cites a
    task or plan yet) reads as the empty tuple. But a *present* value that is not
    a list, or a list carrying any non-``str`` member, is a fault and returns
    ``None`` so the whole episode is dropped rather than silently losing part of
    its causal provenance. This backs the causal-reach row's "validated
    fail-closed" contract in ``docs/conscious-access.md`` — unlike the fail-open
    :func:`_str_tuple` used for the non-provenance ``blind_spots`` /
    ``redacted_item_ids`` fields.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    if not all(isinstance(member, str) for member in value):
        return None
    return tuple(value)


def _parse_item(data: object) -> WorkspaceItem | None:
    """Reconstruct a :class:`WorkspaceItem`, or ``None`` on any structural fault."""
    if not isinstance(data, dict):
        return None
    item_id = _opt_str(data.get("item_id"))
    source_kind = _member(data.get("source_kind"), _SOURCE_KINDS)
    source_ref = _opt_str(data.get("source_ref"))
    observed_at = _opt_str(data.get("observed_at"))
    trust = _member(data.get("trust"), _TRUST_CLASSES)
    confidence = _as_float(data.get("confidence"))
    summary = _opt_str(data.get("summary"))
    # The recurrence lineage (proposal 130) uses the fail-closed coercer: absent
    # -> () for legacy rows, a valid list -> the tuple, a malformed present value
    # -> None so the corrupt item is dropped by the None-guard below.
    lineage = _str_tuple_strict(data.get("lineage"))
    if (
        item_id is None
        or source_kind is None
        or source_ref is None
        or observed_at is None
        or trust is None
        or confidence is None
        or summary is None
        or lineage is None
    ):
        return None
    return WorkspaceItem(
        item_id=item_id,
        source_kind=cast(SourceKind, source_kind),
        source_ref=source_ref,
        observed_at=observed_at,
        trust=cast(TrustClass, trust),
        confidence=confidence,
        expires_at=_opt_str(data.get("expires_at")),
        summary=summary,
        lineage=lineage,
    )


def _parse_excluded(data: object) -> ExcludedCandidate | None:
    """Reconstruct an :class:`ExcludedCandidate`, or ``None`` on a fault."""
    if not isinstance(data, dict):
        return None
    item_id = _opt_str(data.get("item_id"))
    source_kind = _member(data.get("source_kind"), _SOURCE_KINDS)
    score = _as_float(data.get("score"))
    reason = _member(data.get("reason"), _EXCLUSION_REASONS)
    if item_id is None or source_kind is None or score is None or reason is None:
        return None
    return ExcludedCandidate(
        item_id=item_id,
        source_kind=cast(SourceKind, source_kind),
        score=score,
        reason=cast(ExclusionReason, reason),
    )


def _parse_schema(data: object) -> AttentionSchema | None:
    """Reconstruct an :class:`AttentionSchema`, or ``None`` on a fault."""
    if not isinstance(data, dict):
        return None
    focus = _opt_str(data.get("focus"))
    uncertainty = _as_float(data.get("uncertainty"))
    capacity_used = _as_int(data.get("capacity_used"))
    omitted_count = _as_int(data.get("omitted_count"))
    permission_summary = _opt_str(data.get("permission_summary"))
    if (
        focus is None
        or uncertainty is None
        or capacity_used is None
        or omitted_count is None
        or permission_summary is None
    ):
        return None
    return AttentionSchema(
        focus=focus,
        uncertainty=uncertainty,
        capacity_used=capacity_used,
        omitted_count=omitted_count,
        permission_summary=permission_summary,
        blind_spots=_str_tuple(data.get("blind_spots")),
    )


def _parse_receipt(data: object) -> BroadcastReceipt | None:
    """Reconstruct a :class:`BroadcastReceipt`, or ``None`` on a fault."""
    if not isinstance(data, dict):
        return None
    moment_id = _opt_str(data.get("moment_id"))
    moment_hash = _opt_str(data.get("moment_hash"))
    consumer = _opt_str(data.get("consumer"))
    delivered_at = _opt_str(data.get("delivered_at"))
    if (
        moment_id is None
        or moment_hash is None
        or consumer is None
        or delivered_at is None
    ):
        return None
    return BroadcastReceipt(
        moment_id=moment_id,
        moment_hash=moment_hash,
        consumer=consumer,
        delivered_at=delivered_at,
        redacted_item_ids=_str_tuple(data.get("redacted_item_ids")),
    )


def _parse_tuple[R](
    value: object, parse: Callable[[object], R | None],
) -> tuple[R, ...] | None:
    """Parse a JSON list into a tuple of records, or ``None`` if any row faults.

    A member that *parse* rejects fails the whole collection (the nested record
    is corrupt, so the episode is dropped rather than silently shortened). A
    non-list *value* also fails — the field is required to be a list on disk. The
    ``R`` type parameter ties the returned tuple's element type to *parse*'s
    return type, so mypy rejects passing the wrong record parser (e.g. a
    ``_parse_receipt`` where a ``_parse_item`` belongs).
    """
    if not isinstance(value, list):
        return None
    parsed: list[R] = []
    for member in value:
        record = parse(member)
        if record is None:
            return None
        parsed.append(record)
    return tuple(parsed)


def _parse_moment(data: object) -> ConsciousMoment | None:
    """Reconstruct a :class:`ConsciousMoment`, or ``None`` on any fault."""
    if not isinstance(data, dict):
        return None
    moment_id = _opt_str(data.get("moment_id"))
    continuity = _member(data.get("continuity"), _CONTINUITY_KINDS)
    assembled_at = _opt_str(data.get("assembled_at"))
    policy_version = _opt_str(data.get("policy_version"))
    candidate_hash = _opt_str(data.get("candidate_hash"))
    selection_hash = _opt_str(data.get("selection_hash"))
    max_items = _as_int(data.get("max_items"))
    max_chars = _as_int(data.get("max_chars"))
    items = _parse_tuple(data.get("items"), _parse_item)
    excluded = _parse_tuple(data.get("excluded"), _parse_excluded)
    schema = _parse_schema(data.get("schema"))
    if (
        moment_id is None
        or continuity is None
        or assembled_at is None
        or policy_version is None
        or candidate_hash is None
        or selection_hash is None
        or max_items is None
        or max_chars is None
        or items is None
        or excluded is None
        or schema is None
    ):
        return None
    return ConsciousMoment(
        moment_id=moment_id,
        predecessor_id=_opt_str(data.get("predecessor_id")),
        continuity=cast(ContinuityKind, continuity),
        assembled_at=assembled_at,
        policy_version=policy_version,
        items=items,
        excluded=excluded,
        schema=schema,
        candidate_hash=candidate_hash,
        selection_hash=selection_hash,
        max_items=max_items,
        max_chars=max_chars,
    )


def _parse_conscious_episode_dict(
    data: object, *, source: Path | None = None,
) -> ConsciousEpisode | None:
    """Reconstruct a :class:`ConsciousEpisode` from a decoded row, or ``None``.

    Returns ``None`` on any structural failure — a non-dict row, a missing or
    malformed field, a corrupt nested record, or a *malformed citation list* (the
    fail-closed causal-reach contract; see :func:`_str_tuple_strict`) — so the
    ledger reader can skip it within its bounded scan. Every ``tuple`` field and
    nested record is reconstructed explicitly (never by a field-filter that would
    leave nested members as ``dict``s or ``tuple`` fields as ``list``s). *source*
    is an optional path used only to attribute a warning, mirroring the other
    tolerant JSONL parsers.
    """
    if not isinstance(data, dict):
        return None
    moment = _parse_moment(data.get("moment"))
    receipts = _parse_tuple(data.get("receipts"), _parse_receipt)
    recorded_at = _opt_str(data.get("recorded_at"))
    cited_task_ids = _str_tuple_strict(data.get("cited_task_ids"))
    cited_plan_ids = _str_tuple_strict(data.get("cited_plan_ids"))
    if (
        moment is None
        or receipts is None
        or recorded_at is None
        or cited_task_ids is None
        or cited_plan_ids is None
    ):
        if source is not None:
            logger.warning("Skipping corrupted conscious-episode record in %s", source)
        return None
    return ConsciousEpisode(
        moment=moment,
        receipts=receipts,
        cited_task_ids=cited_task_ids,
        cited_plan_ids=cited_plan_ids,
        recorded_at=recorded_at,
    )


# ---------------------------------------------------------------------------
# The shared reader and the persistence surface
# ---------------------------------------------------------------------------


def load_all_conscious_episodes(data_dir: Path) -> list[ConsciousEpisode]:
    """Return every recorded episode oldest-first — the SQLite seed source.

    Walks ``conscious_episodes.jsonl`` newest-first via
    :func:`~maestro.fileutil.iter_jsonl_reverse`, skipping corrupted records
    within :data:`MAX_CORRUPT_SKIP`, then reverses to oldest-first so seeding the
    SQLite store in chronological order lets its ``AUTOINCREMENT`` ``id``
    reproduce the append order (a later ``ORDER BY id DESC`` read then matches the
    JSONL newest-first walk). A real call site (the
    :class:`~maestro.state_store.SqliteConsciousEpisodeLedger` one-time seed), so
    it does not trip the self-review uncalled-symbol gate. Read-only, zero SDK
    cost.
    """
    episodes = list(_iter_episodes(data_dir, max_corrupt=MAX_CORRUPT_SKIP))
    episodes.reverse()
    return episodes


def _iter_episodes(data_dir: Path, *, max_corrupt: int) -> Iterator[ConsciousEpisode]:
    """Yield episodes newest-first, skipping corrupted records within a bound.

    After *max_corrupt* corrupted records the walk terminates with a partial
    result so a burst of corrupted lines in the file tail never forces a
    full-file scan (mirroring :func:`maestro.deed_ledger._iter_deeds`).
    """
    path = data_dir / CONSCIOUS_EPISODES_FILENAME
    if not path.exists():
        return
    skipped = 0
    for data in iter_jsonl_reverse(path):
        # Pass ``source=path`` so the per-row "skipping corrupted record" warning
        # is emitted once, by the parser (mirroring ``load_all_gate_activations``);
        # ``iter_jsonl_reverse`` only yields dicts, so every skipped row here has
        # failed the parser's structural check and is warned there.
        episode = _parse_conscious_episode_dict(data, source=path)
        if episode is None:
            skipped += 1
            if skipped >= max_corrupt:
                logger.warning(
                    "Stopped scanning %s after skipping %d corrupted records",
                    path, skipped,
                )
                return
            continue
        yield episode


@runtime_checkable
class ConsciousEpisodeBackend(Protocol):
    """The conscious-episode ledger persistence surface (proposal 106 shape).

    One writer and two readers go through this seam so the append-only ledger can
    live in ``conscious_episodes.jsonl`` (default) or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. Mirrors the
    :class:`~maestro.deed_ledger.DeedLedgerBackend` idiom; ``@runtime_checkable``
    enables a structural ``isinstance`` check in the agent's backend-selection
    tests.
    """

    def append(self, episode: ConsciousEpisode) -> None:
        """Append exactly one episode."""
        ...

    def read_recent(self, n: int) -> list[ConsciousEpisode]:
        """Return the *n* most-recent episodes, newest-first."""
        ...

    def read_all(self) -> list[ConsciousEpisode]:
        """Return every recorded episode oldest-first."""
        ...


class JsonlConsciousEpisodeLedger:
    """JSONL-backed :class:`ConsciousEpisodeBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the ledger on
    ``<data_dir>/conscious_episodes.jsonl``. The append is atomic and
    append-only: guarded by :func:`~maestro.fileutil.file_lock` and opened in
    ``"a"`` mode with UTF-8 encoding, so a concurrent writer can never interleave
    a half-line. Reads walk newest-first with the bounded corrupt-record skip.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def append(self, episode: ConsciousEpisode) -> None:
        path = self._data_dir / CONSCIOUS_EPISODES_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path), open(path, "a", encoding="utf-8") as f:
            f.write(_row(episode) + "\n")

    def read_recent(self, n: int) -> list[ConsciousEpisode]:
        if n <= 0:
            return []
        episodes: list[ConsciousEpisode] = []
        for episode in _iter_episodes(self._data_dir, max_corrupt=n):
            episodes.append(episode)
            if len(episodes) >= n:
                break
        return episodes

    def read_all(self) -> list[ConsciousEpisode]:
        # Delegate to the module reader so the "read every episode oldest-first"
        # logic lives in exactly one place (mirroring JsonlDeedLedger delegating
        # to its module functions).
        return load_all_conscious_episodes(self._data_dir)
