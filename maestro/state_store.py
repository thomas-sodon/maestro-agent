"""SQLite repository seam for Maestro's mutable state (proposal 106).

Maestro persists a fleet of hand-rolled JSON/JSONL sidecars, each doing its own
load-modify-write with no shared substrate. This module lays the foundation for
consolidating that fleet onto one SQLite file, starting with the smallest, most
self-contained store: the candidate-task queue.

:class:`StateStore` owns one long-lived :class:`sqlite3.Connection` and exposes
typed task-queue methods; :class:`SqliteTaskQueue` adapts it to the
:class:`~maestro.task_queue.TaskQueueBackend` surface so the agent's
construction seam can select it behind ``[paths] sqlite_state_enabled`` without
any call-site branching. ``sqlite3`` is in the standard library, so this adds no
third-party dependency.

The store is default-off and additive: enabling the flag seeds the store once
from a legacy ``task_queue.json`` (if present) and never deletes that file, so
flipping the flag back off is a clean rollback to the JSON backend.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Sequence, Sized
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar, cast

from maestro.audit_checks import (
    GateActivation,
    _parse_gate_activation_dict,
    load_all_gate_activations,
    load_all_impact_review_surfaced,
)
from maestro.audit_checks_calibration import (
    PremiseObservation,
    _parse_premise_observation_dict,
    load_all_predictions,
    load_all_premise_observations,
)
from maestro.audit_checks_phantom import (
    load_all_orphaned_cure_seen,
    load_all_unconfirmed_landing_seen,
)
from maestro.audit_checks_settlement import (
    SettledVerdictRecord,
    load_all_settled_verdicts,
)
from maestro.conscious_workspace import (
    ConsciousEpisode,
    _parse_conscious_episode_dict,
    episode_to_dict,
    load_all_conscious_episodes,
)
from maestro.cost_tracking import COST_BILLINGS, CostBilling, CostSample, _as_utc
from maestro.deed_ledger import Deed, DeedType, build_deed, load_all_deeds
from maestro.directive_ledger import (
    _MAX_ENTRIES,
    DroppedDirective,
    load_all_dropped_directives,
)
from maestro.escalation_bridge import (
    StanddownRecord,
    _parse_standdown_entry,
    load_all_escalation_standdowns,
)
from maestro.lever_capture import LeverAttempt, load_all_lever_attempts
from maestro.memory import (
    AuditResult,
    Memory,
    QuestionResolution,
    RejectionRecord,
    VersionRecord,
    _parse_audit_dict,
    _parse_question_resolution_dict,
    _parse_rejection_dict,
    load_all_audit_history,
    load_all_question_resolutions,
    load_all_rejection_history,
    load_all_versions,
)
from maestro.outcome_grounding import load_all_serviced_merge_requests
from maestro.pr_ledger import (
    PRLedgerEntry,
    _entry_from_raw,
    entries_snapshot,
    load_all_pr_ledger_entries,
)
from maestro.remediation_tracker import (
    FetchTruthRecord,
    ObligationFork,
    ObligationForkRecord,
    PreflightPassRecord,
    ProvenanceRecord,
    StandDownRow,
    StandDownVerdict,
    TrackerState,
    _parse_tracker_payload,
    load_all_fetch_truth_markers,
    load_all_obligation_forks,
    load_all_preflight_passes,
    load_all_provenance_records,
    load_all_stand_down_records,
    load_tracker_state,
)
from maestro.search import _FTS5_TOKENIZE_SPEC, tokenize
from maestro.settlement_record_reconciler import (
    SettlementReconciliationRecord,
    load_all_settlement_reconciliations,
)
from maestro.stats import (
    BacklogSnapshot,
    build_backlog_snapshot,
    load_all_backlog_snapshots,
)
from maestro.task_queue import QueuedTask, TaskQueue, normalize_task

logger = logging.getLogger(__name__)

#: Closure return type for :meth:`SqlitePRLedger.mutate` (proposal 106).
T = TypeVar("T")

#: The heterogeneous seed-argument type carried through
#: :meth:`StateStore._seed_sized` — one of the per-store legacy loaders'
#: sized return values (``list`` / ``dict`` / ``set``), so the seed call and
#: its row count share one inferred type (proposal 106).
_SeedArg = TypeVar("_SeedArg", bound=Sized)


class StateStoreSchemaError(RuntimeError):
    """A ``state.db`` table cannot be brought up to its declared schema.

    Raised from :meth:`StateStore.__init__` so an unrepairable table is a named
    startup failure at the ``StateStore(...)`` call site — the chokepoint the
    schema-normalisation invariant promises — instead of a bare
    ``sqlite3.OperationalError`` on the first queue statement mid-loop.
    """


#: DDL for the task-queue table. Columns mirror :class:`QueuedTask` fields plus
#: an explicit integer ``position`` that preserves insertion / FIFO order
#: independent of rowid churn. The optional string fields are nullable TEXT and
#: ``acceptance_criteria`` / ``workspace_item_ids`` are JSON-serialized lists
#: (or NULL). ``workspace_moment_id`` / ``workspace_item_ids`` carry the
#: conscious-access citation (proposal 130). This is the one table in the store
#: whose dataclass maps to per-field typed columns rather than a single JSON
#: ``data`` blob, so an added field needs a schema step: since
#: ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, the
#: ``ALTER TABLE`` back-fill in :meth:`StateStore.__init__` adds every nullable
#: column :data:`_TASK_QUEUE_COLUMNS` declares but an older table lacks, and
#: raises :class:`StateStoreSchemaError` if any declared column — including a
#: ``NOT NULL`` one, which SQLite cannot add in place — remains missing.
_TASK_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS task_queue (
    position INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    linked_question TEXT,
    rationale TEXT,
    created_at TEXT NOT NULL,
    last_touched_at TEXT NOT NULL,
    acceptance_criteria TEXT,
    workspace_moment_id TEXT,
    workspace_item_ids TEXT
)
"""

#: DDL for a tiny key/value metadata table. Currently holds only the one-time
#: task-queue seed marker: a durable, per-DB record that the seed-from-legacy
#: -JSON step has already run, so a legitimately-drained queue is never
#: re-seeded from the frozen legacy JSON on a later restart.
_METADATA_DDL = """
CREATE TABLE IF NOT EXISTS store_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

#: Metadata key stamped once the task-queue seed step has run.
_TASK_QUEUE_SEEDED_KEY = "task_queue_seeded"

#: Metadata key stamped once the deed-ledger seed step has run.
_DEED_LEDGER_SEEDED_KEY = "deed_ledger_seeded"

#: Metadata key stamped once the owned-backlog seed step has run.
_OWNED_BACKLOG_SEEDED_KEY = "owned_backlog_seeded"

#: Metadata key stamped once the PR-provenance seed step has run.
_PR_PROVENANCE_SEEDED_KEY = "pr_provenance_seeded"

#: Metadata key stamped once the obligation-fork seed step has run.
_OBLIGATION_FORK_SEEDED_KEY = "obligation_fork_seeded"

#: Metadata key stamped once the fetch-truth seed step has run.
_FETCH_TRUTH_SEEDED_KEY = "fetch_truth_seeded"

#: Metadata key stamped once the preflight-pass seed step has run.
_PREFLIGHT_PASSES_SEEDED_KEY = "preflight_passes_seeded"

#: Metadata key stamped once the dropped-directive seed step has run.
_DROPPED_DIRECTIVES_SEEDED_KEY = "dropped_directives_seeded"

#: Metadata key stamped once the settled-verdicts seed step has run.
_SETTLED_VERDICTS_SEEDED_KEY = "settled_verdicts_seeded"

#: Metadata key stamped once the stand-down-verdict seed step has run.
_STAND_DOWN_VERDICTS_SEEDED_KEY = "stand_down_verdicts_seeded"

#: Metadata key stamped once the unconfirmed-own-landing seed step has run.
_UNCONFIRMED_LANDING_SEEDED_KEY = "unconfirmed_landing_seeded"

#: Metadata key stamped once the orphaned-cure seed step has run.
_ORPHANED_CURE_SEEDED_KEY = "orphaned_cure_seeded"

#: Metadata key stamped once the impact-review surfaced-set seed step has run.
_IMPACT_REVIEW_SURFACED_SEEDED_KEY = "impact_review_surfaced_seeded"

#: Metadata key stamped once the mergeable-flip serviced-marker seed step has run.
_MERGEABLE_SERVICED_SEEDED_KEY = "mergeable_serviced_seeded"

#: Metadata key stamped once the settlement-reconciliation seed step has run.
_SETTLEMENT_RECONCILIATIONS_SEEDED_KEY = "settlement_reconciliations_seeded"

#: Metadata key stamped once the lever-attempts seed step has run.
_LEVER_ATTEMPTS_SEEDED_KEY = "lever_attempts_seeded"

#: Metadata key stamped once the audit-history seed step has run.
_AUDIT_HISTORY_SEEDED_KEY = "audit_history_seeded"

#: Metadata key stamped once the rejection-history seed step has run.
_REJECTION_HISTORY_SEEDED_KEY = "rejection_history_seeded"

#: Metadata key stamped once the question-resolution seed step has run.
_QUESTION_RESOLUTIONS_SEEDED_KEY = "question_resolutions_seeded"

#: Metadata key stamped once the gate-activation seed step has run.
_GATE_ACTIVATIONS_SEEDED_KEY = "gate_activations_seeded"

#: Metadata key stamped once the prediction-ledger seed step has run.
_PREDICTIONS_SEEDED_KEY = "predictions_seeded"

#: Metadata key stamped once the premise-observation seed step has run.
_PREMISE_OBSERVATIONS_SEEDED_KEY = "premise_observations_seeded"

#: Metadata key stamped once the version-archive seed step has run.
_VERSION_ARCHIVE_SEEDED_KEY = "version_archive_seeded"

#: Metadata key stamped once the per-PR remediation-ledger seed step has run.
_PR_LEDGER_SEEDED_KEY = "pr_ledger_seeded"

#: Metadata key stamped once the remediation-tracker seed step has run.
_REMEDIATION_TRACKER_SEEDED_KEY = "remediation_tracker_seeded"

#: Metadata key stamped once the escalation-standdown seed step has run.
_ESCALATION_STANDDOWNS_SEEDED_KEY = "escalation_standdowns_seeded"

#: Seed marker for the conscious-episode ledger (proposal 130).
_CONSCIOUS_EPISODES_SEEDED_KEY = "conscious_episodes_seeded"

#: Metadata key holding the remediation tracker's stored ``schema_version`` —
#: the wrapper field the JSON sidecar carries inline. Kept out of the per-PR
#: table so it survives a whole-map replace that leaves no rows, preserving the
#: pre-v2 version a legacy seed must carry for the one-time #2796 merge-block
#: reconciliation to still fire once after the flag flip.
_REMEDIATION_TRACKER_VERSION_KEY = "remediation_tracker_schema_version"

#: Busy-timeout (milliseconds) installed on every :class:`StateStore` connection.
#: When ``[paths] sqlite_state_enabled`` is on, several connections share the one
#: ``state.db`` file — the SDK write path, the agent's readers, the phase-cost report, and
#: the ``_mcp-serve`` subprocess — so lock contention must *wait and retry*
#: rather than fail fast with "database is locked". Past the timeout the
#: degradation is no longer uniform: the *observational* readers still absorb it
#: (``send_status_update`` reports no rate, ``current_burn_rate_usd_per_hour``
#: reads 0.0) and the writer drops the sample, but the burn-rate throttle
#: *decision* read — ``LifecycleController.compute_budget_throttle_multiplier``
#: — deliberately propagates (#3633), crashing the loop into ``handle_crash``
#: rather than mapping a measurement fault onto the neutral tuple that would
#: release an engaged throttle. That is the accepted tradeoff: a sustained
#: contention stall past 5s is treated as a defect worth surfacing, not as
#: "spend is fine". ``sqlite3
#: .connect`` already defaults this to 5000ms for the path / ``":memory:"``
#: branches; setting it explicitly also normalizes an *injected* connection built
#: with a shorter (or zero) timeout, and records the multi-connection design as
#: intentional (proposal 106).
_BUSY_TIMEOUT_MS = 5000

#: DDL for the cost-sample time-series (proposal 106 Phase B Step 3). An
#: autoincrement ``id`` preserves insertion / FIFO order (and, being strictly
#: monotonic across the append-only lifetime, doubles as the TTL-cache version
#: key). ``timestamp`` is the ISO string stored verbatim — readers re-parse it
#: through ``cost_tracking._as_utc`` exactly as the JSONL path does — beside a
#: numeric ``ts_epoch`` companion, derived at write time through that same
#: helper so a naive stamp lands in the UTC domain the reader's cutoff uses,
#: letting windowed reads compare epochs directly and sidestep ISO
#: lexicographic pitfalls. The remaining columns mirror
#: :class:`~maestro.cost_tracking.CostSample`: the two bool flags as ``INTEGER``
#: 0/1 and the optional ``provider``, ``effort`` and ``model`` as nullable
#: ``TEXT`` (``effort``/``model`` added by proposal 140; an older table gains
#: them through the additive back-fill in :class:`StateStore`).
_COST_SAMPLES_DDL = """
CREATE TABLE IF NOT EXISTS cost_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ts_epoch REAL NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    caller TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    estimated INTEGER NOT NULL,
    unmetered INTEGER NOT NULL,
    provider TEXT,
    effort TEXT,
    model TEXT,
    billing TEXT
)
"""

#: Composite index backing the windowed range read (``load_cost_samples_since``'s
#: ``WHERE ts_epoch >= ? ORDER BY ts_epoch, id``). Without it SQLite full-scans
#: the append-only, never-pruned ``cost_samples`` table on every burn-rate
#: throttle read (the per-iteration hot path), a silent regression against the
#: JSONL reader's break-early O(window) tail walk. A *plain* ``ts_epoch`` index
#: would not help: ordering by the ``id`` rowid is free during a table scan, so
#: the planner keeps the full scan rather than pay for an index range plus a
#: sort. Leading the ordering with ``ts_epoch`` (``id`` only as a tiebreak) lets
#: the composite index serve both the range seek and the order, so the read
#: touches only the in-window rows — restoring that bound.
_COST_SAMPLES_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_cost_samples_ts_epoch "
    "ON cost_samples(ts_epoch, id)"
)

#: DDL for the persistent journal-search FTS5 index (proposal 106). A
#: rebuildable derived cache — a self-healing projection of the append-only
#: ``journal.jsonl`` — that retires the last per-call FTS5 rebuild on the loop's
#: one deliberately-unbounded corpus: :meth:`StateStore.rank_journal_search`
#: tops the index up with only the entries appended since the last search
#: instead of re-tokenizing the whole history. ``rowid`` equals the (filtered)
#: corpus position and ``content`` holds the space-joined :func:`tokenize`
#: stream, exactly as the ephemeral ranker inserts, so bm25 corpus statistics
#: are identical. The tokenizer clause is shared verbatim with the ephemeral
#: ``_FTS5_CREATE`` via :data:`~maestro.search._FTS5_TOKENIZE_SPEC` — any drift
#: would silently desync the two rankings. ``IF NOT EXISTS`` because, unlike the
#: ephemeral ``:memory:`` table, this one is created on every
#: :class:`StateStore` construction.
_JOURNAL_SEARCH_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS journal_search USING fts5("
    f'content, tokenize = "{_FTS5_TOKENIZE_SPEC}")'
)

#: Metadata key holding the journal-search index watermark: how many
#: filtered-corpus entries are indexed into ``journal_search``, in append order
#: (``rowid`` == corpus position). Unlike the ``*_seeded`` one-shot markers this
#: is a *mutable count* — advanced on every append-only top-up, reset on a
#: rebuild — so a reconcile indexes only the appended tail ``corpus[watermark:]``
#: rather than the whole corpus. Written in the same ``BEGIN IMMEDIATE``
#: transaction as the index rows, so the count and the table can never disagree.
#: Versioned: the corpus was redefined to inner thought only, so a pre-split
#: watermark (under the unversioned key) must read absent and force a rebuild.
_JOURNAL_SEARCH_INDEXED_COUNT_KEY = "journal_search_indexed_count_v2"

#: DDL for the deed ledger — the append-only record of fired terminal acts
#: (``gh pr close`` disposals, delivered handoffs, merges, owned prunes). Columns
#: mirror :class:`~maestro.deed_ledger.Deed`: an ``AUTOINCREMENT`` ``id``
#: preserves append / FIFO order (so a newest-first read is ``ORDER BY id DESC``),
#: ``pr_number`` is nullable (an owned prune ends no single PR), and ``timestamp``
#: is the verbatim ISO string (lexical order equals chronological order for the
#: ``since`` window). Unlike the indexed ``cost_samples`` time-series, deeds are
#: terminal-act-rare — written only when a lineage ends, never on a per-SDK-call
#: hot path — so no supporting index is added: the whole-table count and the
#: small recent-N read never touch enough rows to need one.
_DEED_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS deeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deed_type TEXT NOT NULL,
    pr_number INTEGER,
    detail TEXT NOT NULL,
    timestamp TEXT NOT NULL
)
"""

#: DDL for the lever-attempts ledger — the append-only record of owned
#: terminal-lever fires (today only ``mark_plan_obsolete`` from the two
#: backlog-pruning sweeps). Columns mirror :class:`~maestro.lever_capture
#: .LeverAttempt`: an ``AUTOINCREMENT`` ``id`` preserves append / FIFO order (so
#: a later ``ORDER BY id ASC`` read reproduces the JSONL forward walk), ``args``
#: is the JSON-encoded ``list[str]`` (the target ``plan_id`` and disposal
#: reason), and the two booleans store as ``INTEGER`` 0/1. Append-only, no unique
#: key, one row per fire. Like the deed ledger these fires are terminal-act-rare
#: — never on a per-SDK-call hot path — so no supporting index is added: the
#: whole-table selector read never touches enough rows to need one.
_LEVER_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS lever_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lever TEXT NOT NULL,
    args TEXT NOT NULL,
    verified INTEGER NOT NULL,
    result INTEGER NOT NULL
)
"""

#: DDL for the audit-history ledger (proposal 106) — the append-only record of
#: every ``audit_self_model`` cycle's outcome, one
#: :class:`~maestro.memory.AuditResult` row per cycle. The whole result is stored as a single ``json.dumps(asdict(result))``
#: blob in ``data`` (an ``AUTOINCREMENT`` ``id`` preserves append / FIFO order,
#: so a later ``ORDER BY id ASC`` read reproduces the JSONL forward walk):
#: ``AuditResult`` gains fields over time and carries a nested ``findings`` list,
#: so a JSON blob round-trips the row exactly and additive fields never force a
#: table migration. Append-only, no unique key, one row per cycle. Audit cycles
#: are contemplation-rare — never on a per-SDK-call hot path — so no supporting
#: index is added.
_AUDIT_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS audit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the rejection-history ledger (proposal 106 Phase B Step 5). One row
#: per human-rejected evolution plan, stored as a single
#: ``json.dumps(asdict(record))`` blob keyed by an ``AUTOINCREMENT`` ``id`` — so
#: additive ``RejectionRecord`` fields never force a table migration, matching
#: the audit-history fold. Append-only, no unique key, one row per
#: rejection. Rejections are reflection-rare — never on a per-SDK-call hot path
#: — so no supporting index is added.
_REJECTION_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS rejection_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the question-resolution ledger (proposal 106). One row per
#: ``(question, finding)`` pair fed to ``update_self_model``, stored as a single
#: ``json.dumps(asdict(record))`` blob keyed by an ``AUTOINCREMENT`` ``id`` — so
#: additive ``QuestionResolution`` fields (e.g. the later ``refined_to``) never
#: force a table migration, matching the audit- and rejection-history
#: folds. Append-only, no unique key, one row per resolution. Resolutions are
#: written once per contemplation-fed pair — never on a per-SDK-call hot path —
#: so no supporting index is added.
_QUESTION_RESOLUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS question_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the gate-activation trace ledger (proposal 106). One row per gate
#: firing (:class:`~maestro.audit_checks.GateActivation`), stored as a single
#: ``json.dumps(asdict(record))`` blob keyed by an ``AUTOINCREMENT`` ``id`` — so
#: additive ``GateActivation`` fields never force a table migration, matching the
#: audit-, rejection-, and question-resolution-history folds. Append-only,
#: no unique key, one row per firing: a pure append trace with no keyed-replace or
#: retraction shape. Firings happen at task-generation / remediation cadence —
#: never on a per-SDK-call hot path — so no supporting index is added.
_GATE_ACTIVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS gate_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the prediction sensor ledger (proposal 106 Phase B Step 5). One row
#: per prediction write, stored as a single ``json.dumps(record)`` blob keyed by
#: an ``AUTOINCREMENT`` ``id`` — the three heterogeneous row shapes (task-outcome
#: predictions, priced-reflection self-predictions, and their deferred verdicts)
#: round-trip verbatim, so additive fields never force a table migration. A pure
#: append trace with no keyed-replace or retraction shape: one row per write,
#: append-only, ``ORDER BY id ASC`` reproducing the JSONL forward (oldest-first)
#: order the readers take a tail of. Writes happen at reflection / task cadence —
#: never on a per-SDK-call hot path — so no supporting index is added.
_PREDICTIONS_DDL = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the premise-accuracy leading indicator (issue #2304). One row per
#: accepted task's premise measurement, stored as a single
#: ``json.dumps(asdict(record))`` blob keyed by an ``AUTOINCREMENT`` ``id`` — so
#: additive :class:`~maestro.audit_checks_calibration.PremiseObservation` fields never force
#: a table migration, matching :data:`_GATE_ACTIVATIONS_DDL`. A pure append
#: trace with no keyed-replace or retraction shape: one row per accept,
#: append-only, ``ORDER BY id ASC`` reproducing the JSONL forward (oldest-first)
#: order the CLI reader takes a tail of. Writes happen at task-generation
#: cadence — never on a per-SDK-call hot path — so no supporting index is added.
_PREMISE_OBSERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS premise_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the conscious-episode ledger (proposal 130). One row per persisted
#: :class:`~maestro.conscious_workspace.ConsciousEpisode`, stored as a single
#: ``json.dumps(episode_to_dict(record))`` blob keyed by an ``AUTOINCREMENT``
#: ``id`` — so additive episode fields never force a table migration, matching
#: :data:`_GATE_ACTIVATIONS_DDL`. A pure append trace with no keyed-replace or
#: retraction shape: one row per moment, append-only, ``ORDER BY id ASC``
#: reproducing the JSONL forward (oldest-first) order the readers take a tail of.
#: Writes happen at attend cadence — never on a per-SDK-call hot path — so no
#: supporting index is added.
_CONSCIOUS_EPISODES_DDL = """
CREATE TABLE IF NOT EXISTS conscious_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the owned-backlog snapshot ledger (proposal 106 Phase B Step 5). One
#: row per contemplation cycle recording the owned ``pending_review`` backlog
#: size, whose signed cycle-over-cycle delta is the metric the backlog sensor
#: tracks. An ``AUTOINCREMENT`` ``id`` preserves append / FIFO order (so a
#: newest-first read is ``ORDER BY id DESC``, matching the JSONL reverse read)
#: and ``timestamp`` is the verbatim ISO string. Unlike the indexed
#: ``cost_samples`` time-series these snapshots are cycle-rare — written once per
#: contemplation, read only as the recent-2 delta — so no supporting index is
#: added: the small recent-N read never touches enough rows to need one.
_OWNED_BACKLOG_DDL = """
CREATE TABLE IF NOT EXISTS backlog_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration INTEGER NOT NULL,
    count INTEGER NOT NULL,
    timestamp TEXT NOT NULL
)
"""

#: DDL for the PR-admission provenance ledger (proposal 106 Phase B Step 5). One
#: row per ``pending_pr_requests`` re-admission recording the PR number and the
#: admission channel; the ``PR-READMISSION`` sensor reads a trailing window of
#: these rows to count how often the router re-feeds the same PR rather than
#: landing it once. An ``AUTOINCREMENT`` ``id`` preserves append / FIFO order (so
#: a newest-first read is ``ORDER BY id DESC``, matching the JSONL trailing
#: -window walk). There is no timestamp column — the on-disk row carries none, so
#: the record is ``(pr_number, channel)`` only. Unlike the indexed
#: ``cost_samples`` time-series these admissions are tend-lap-rare — written only
#: when the tend loop re-admits an open PR, read only as a bounded recent-N scan
#: — so no supporting index is added: the small recent-N read never touches
#: enough rows to need one.
_PR_PROVENANCE_DDL = """
CREATE TABLE IF NOT EXISTS provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    channel TEXT NOT NULL
)
"""

#: DDL for the obligation-fork marker ledger (proposal 106 Phase B Step 5). One
#: row per remediation task/turn recording the HEAD-vs-``origin/<branch>`` fork a
#: lap observed — the two SHAs, the branch, and whether the remote is an ancestor
#: of HEAD — so :func:`~maestro.remediation_tracker.gate_remediation_verdict` can
#: tell an owed push (HEAD genuinely ahead) from an external merge (HEAD level
#: with origin). An ``AUTOINCREMENT`` ``id`` preserves append order (so a
#: last-match read is ``ORDER BY id DESC``, matching the JSONL reader's last
#: -matching-row walk). ``remote_is_ancestor`` is stored as ``INTEGER`` 0/1. The
#: ``UNIQUE(task_id, pr_number)`` constraint reproduces the JSONL writer's
#: first-wins-per-``(task_id, pr_number)`` idempotency: a second ``INSERT OR
#: IGNORE`` for the same pair is a no-op, exactly as the writer's pre-append scan
#: skips a duplicate. Like the tend-lap-rare ``provenance`` ledger these markers
#: are written at most once per remediation turn and read as a single keyed
#: lookup, so no supporting index beyond the unique key is added.
_OBLIGATION_FORK_DDL = """
CREATE TABLE IF NOT EXISTS obligation_forks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    remote_sha TEXT NOT NULL,
    branch TEXT NOT NULL,
    remote_is_ancestor INTEGER NOT NULL,
    UNIQUE(task_id, pr_number)
)
"""

#: DDL for the fetch-truth marker ledger (proposal 106 Phase B Step 5). Each row
#: records that a remediation task/turn ran the fetch-before-word action-zero —
#: ``git fetch`` + ``git merge-base --is-ancestor`` — whose *is_ancestor* verdict
#: :func:`~maestro.remediation_tracker.gate_remediation_verdict` reads to prove a
#: status word did not precede git's answer. Unlike the sibling ``obligation
#: _forks`` ledger this has **no** unique key: its writer
#: (:func:`~maestro.remediation_tracker.record_fetch_truth`) appends one row per
#: call rather than deduping per ``(task_id, pr_number)``, so the store must too.
#: ``is_ancestor`` is stored as ``INTEGER`` 0/1; ``head_sha`` defaults to ``''``
#: for the legacy three-field marker whose origin head could not be resolved. The
#: two reads are keyed ``EXISTS`` lookups (any marker for a ``task_id``; a
#: ``(pr_number, head_sha)`` pair whose ``is_ancestor`` is 0), so no supporting
#: index beyond the primary key is added.
_FETCH_TRUTH_DDL = """
CREATE TABLE IF NOT EXISTS fetch_truth_markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    is_ancestor INTEGER NOT NULL,
    head_sha TEXT NOT NULL DEFAULT ''
)
"""

#: DDL for the preflight-pass ledger (proposal 106 Phase B Step 5). Unlike the
#: append-only marker ledgers this is a per-PR *keyed replace*: ``pr_number`` is
#: the PRIMARY KEY and the sole read is an exact ``(pr_number, head_sha)`` match,
#: so a plain ``INSERT OR REPLACE`` reproduces the legacy JSON dict's "latest
#: head wins" and no supporting index beyond the primary key is added.
_PREFLIGHT_PASSES_DDL = """
CREATE TABLE IF NOT EXISTS preflight_passes (
    pr_number INTEGER PRIMARY KEY,
    head_sha TEXT NOT NULL
)
"""

#: DDL for the dropped-directive replay ledger (proposal 106 Phase B Step 5).
#: Unlike the append-only marker ledgers and the keyed-replace preflight store,
#: this is a *bounded read-modify-write*: ``offer`` appends then trims to
#: :data:`~maestro.directive_ledger._MAX_ENTRIES`, and ``replay`` prunes dead
#: rows and consumes the oldest live one. ``id INTEGER PRIMARY KEY AUTOINCREMENT``
#: reproduces the FIFO order the JSONL append order carried; ``pr_numbers`` and
#: ``shas`` are JSON-encoded lists mirroring the JSONL row. No supporting index
#: beyond the primary key — every read loads the whole (``_MAX_ENTRIES``-bounded)
#: table.
_DROPPED_DIRECTIVES_DDL = """
CREATE TABLE IF NOT EXISTS dropped_directives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    pr_numbers TEXT NOT NULL,
    shas TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""

#: DDL for the settled-verdicts pre-filter (proposal 106 Phase B Step 5). Like
#: the preflight store this is a per-PR *keyed replace*: ``pr_number`` is the
#: PRIMARY KEY, so an ``INSERT OR REPLACE`` per record reproduces the JSONL
#: ledger's settle/retract last-row-wins collapse. ``settled`` is the collapse
#: axis (1 settle / 0 retraction) and ``kind`` the merged/closed discriminator
#: the base-advance-race sensor reads; ``settled_at_iteration`` /
#: ``retracted_at_iteration`` / ``verdict_task_id`` are nullable provenance. No
#: supporting index beyond the primary key — both reads scan on ``settled``.
_SETTLED_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS settled_verdicts (
    pr_number INTEGER PRIMARY KEY,
    settled INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    settled_at_iteration INTEGER,
    retracted_at_iteration INTEGER,
    verdict_task_id TEXT
)
"""

#: DDL for the stand-down verdict ledger (proposal 106 Phase B Step 5). Like the
#: ``fetch_truth_markers`` ledger it is *append-only, no unique key* — one row per
#: call, carrying two on-disk shapes in one table: a *verdict* row (``head_sha`` +
#: ``recorded_at`` set, ``released_at`` NULL) and a head-agnostic *release* row
#: (``released_at`` set, ``head_sha`` / ``recorded_at`` NULL). ``id INTEGER PRIMARY
#: KEY AUTOINCREMENT`` preserves the JSONL append order the release-aware,
#: last-write-wins scan depends on; ``pr`` is nullable so a corrupt pr-less row is
#: excluded from the keyed reads exactly as the JSONL scans skip it;
#: ``branch_ahead_of_origin`` is a nullable 0/1 reachability flag. The reads run
#: the same Python scan the JSONL adapter does (mirroring
#: :class:`SqliteDroppedDirectiveLedger`'s policy-in-Python approach), so no
#: supporting index beyond the primary key is added.
_STAND_DOWN_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS stand_down_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr INTEGER,
    head_sha TEXT,
    recorded_at TEXT,
    released_at TEXT,
    branch_ahead_of_origin INTEGER
)
"""

#: DDL for the unconfirmed-own-landing first-seen sidecar (proposal 106 Phase B
#: Step 5). A keyed ``{sha: first_seen_iteration}`` map: ``sha`` is the PRIMARY
#: KEY and ``prune_and_touch`` rewrites the whole (SHA-set-bounded) table each
#: cycle via an atomic ``DELETE`` + ``executemany`` INSERT, mirroring
#: :meth:`StateStore.replace_task_queue`. No supporting index beyond the primary
#: key — every read loads the whole table.
_UNCONFIRMED_LANDING_DDL = """
CREATE TABLE IF NOT EXISTS unconfirmed_landing_seen (
    sha TEXT PRIMARY KEY,
    first_seen_iteration INTEGER NOT NULL
)
"""

#: DDL for the orphaned-cure first-seen sidecar (proposal 106 Phase B Step 5).
#: A keyed ``{plan_id: first_seen_iteration}`` map — the direct analogue of
#: ``unconfirmed_landing_seen``: ``plan_id`` is the PRIMARY KEY and
#: ``prune_and_touch`` rewrites the whole (implemented-set-bounded) table each
#: cycle via an atomic ``DELETE`` + ``executemany`` INSERT. Unlike its sibling,
#: the map has a second read-only consumer (the retirement sweep), served by a
#: plain ``load``. No supporting index beyond the primary key — every read loads
#: the whole table.
_ORPHANED_CURE_DDL = """
CREATE TABLE IF NOT EXISTS orphaned_cure_seen (
    plan_id TEXT PRIMARY KEY,
    first_seen_iteration INTEGER NOT NULL
)
"""

#: DDL for the impact-review surfaced-set sidecar (proposal 106 Phase B Step 5).
#: An append-only ``set[str]`` of surfaced ``VersionRecord`` ids — the last audit
#: surfaced-set sidecar to fold. The single ``version_id`` PRIMARY KEY *is* the
#: set (giving idempotent membership); ``record`` unions new ids via
#: ``INSERT OR IGNORE`` and ``load`` reads the whole column. Unlike its keyed-map
#: siblings the set is never pruned, so there is no delete path.
_IMPACT_REVIEW_SURFACED_DDL = """
CREATE TABLE IF NOT EXISTS impact_review_surfaced (
    version_id TEXT PRIMARY KEY
)
"""

#: DDL for the mergeable-flip serviced-marker ledger (proposal 106 Phase B Step 5).
#: The ``{pr_number: set[head_sha]}`` map recording which PR heads already had a
#: named merge-request delivered — an append-only, per-consumer set like
#: ``impact_review_surfaced`` but *keyed on a pair*: the composite
#: ``PRIMARY KEY (pr_number, head_sha)`` *is* the per-PR head set, so an
#: ``INSERT OR IGNORE`` union gives idempotent membership and no supporting index
#: is added. Never pruned, so there is no delete path.
_MERGEABLE_SERVICED_DDL = """
CREATE TABLE IF NOT EXISTS mergeable_serviced (
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    PRIMARY KEY (pr_number, head_sha)
)
"""

#: DDL for the on-settlement record-reconciler provenance ledger (proposal 106).
#: One row per reconciler-driven store change for a settled PR — the append-only,
#: operator-inspectable trace that ``settlement_reconciliations.jsonl`` held. The
#: ``AUTOINCREMENT`` ``id`` preserves append/FIFO order (so a read-all is
#: ``ORDER BY id ASC``), ``cleared`` is stored ``INTEGER`` 0/1, and ``detail`` is
#: nullable. Append-only with no hot-path reader and no unique key, so no
#: supporting index is added.
_SETTLEMENT_RECONCILIATIONS_DDL = """
CREATE TABLE IF NOT EXISTS settlement_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    reconciler TEXT NOT NULL,
    cleared INTEGER NOT NULL,
    detail TEXT,
    at_iteration INTEGER NOT NULL
)
"""

#: DDL for the version-archive ledger (proposal 106). One row per merged owned
#: evolution PR (:class:`~maestro.memory.VersionRecord`), stored as a single
#: ``json.dumps(asdict(record))`` blob keyed by an ``AUTOINCREMENT`` ``id`` — so
#: additive ``VersionRecord`` fields never force a table migration, matching the
#: audit-, rejection-, question-resolution-, gate-activation-, and
#: prediction-history folds. Append-only, no unique key, one row per merged
#: plan: a pure append ledger with no keyed-replace or retraction shape.
#: Versions are recorded once per merged PR — never on a per-SDK-call hot path —
#: so no supporting index is added.
_VERSION_ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS version_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL
)
"""

#: DDL for the per-PR remediation ledger (proposal 106). Like the preflight and
#: settled-verdicts stores this is a per-PR *keyed replace* — ``pr_number`` is
#: the PRIMARY KEY — but the whole :class:`~maestro.pr_ledger.PRLedgerEntry` is
#: stored as a single ``json.dumps(asdict(entry))`` blob in ``data`` (matching
#: the ``audit_history`` / ``version_archive`` blob precedent), so the eleven
#: entry fields (and any additive ones) never force a table migration. Every
#: mutation is a whole-map atomic replace (:meth:`StateStore.replace_pr_ledger`),
#: so no supporting index beyond the primary key is added.
_PR_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS pr_ledger (
    pr_number INTEGER PRIMARY KEY,
    data TEXT NOT NULL
)
"""

#: DDL for the per-PR remediation-repetition tracker (proposal 106). The tracker
#: holds four parallel per-PR maps (no-op outcome lists, the outstanding
#: review-item baseline, repetition-gate skips, merge-block skips); rather than
#: four tables or four columns, each PR key stores one JSON blob carrying exactly
#: the facets present for it, so an empty-but-present facet round-trips and an
#: additive fifth map never forces a table migration. The wrapper's
#: ``schema_version`` lives in ``store_metadata`` under
#: :data:`_REMEDIATION_TRACKER_VERSION_KEY`, not here — it is state-wide, and a
#: whole-map replace that empties the table must not lose it. Every mutation is a
#: whole-map atomic replace (:meth:`StateStore.replace_remediation_tracker`),
#: mirroring the JSON path's whole-file ``atomic_write``, so no supporting index
#: beyond the primary key is added.
_REMEDIATION_TRACKER_DDL = """
CREATE TABLE IF NOT EXISTS remediation_tracker (
    pr_key TEXT PRIMARY KEY,
    data TEXT NOT NULL
)
"""

#: DDL for the escalation-standdown ledger (proposal 106). Like ``pr_ledger``
#: this is a per-key *keyed replace* storing the whole
#: :class:`~maestro.escalation_bridge.StanddownRecord` as a single
#: ``json.dumps`` blob in ``data``, so the optional ``expires_at`` (and any
#: additive field) never forces a table migration. The key column is ``number``,
#: not ``pr_number``: claimed-escalation records (proposal 090) key on plain
#: issue numbers, so the column names the shared numeric keyspace. Every
#: mutation is a whole-map atomic replace inside one ``BEGIN IMMEDIATE``
#: transaction (:meth:`StateStore.mutate_escalation_standdowns`), so no
#: supporting index beyond the primary key is added.
_ESCALATION_STANDDOWNS_DDL = """
CREATE TABLE IF NOT EXISTS escalation_standdowns (
    number INTEGER PRIMARY KEY,
    data TEXT NOT NULL
)
"""

#: Column order shared by the cost-sample INSERT and ``_cost_sample_to_row``
#: (sans the auto-assigned ``id``).
_COST_SAMPLE_COLUMNS = (
    "timestamp",
    "ts_epoch",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "caller",
    "duration_ms",
    "estimated",
    "unmetered",
    "provider",
    "effort",
    "model",
    "billing",
)

#: Nullable ``TEXT`` columns added to ``cost_samples`` after it first shipped
#: (proposal 140; ``billing`` proposal 152). ``StateStore`` back-fills any the
#: live table lacks.
_COST_SAMPLE_ADDITIVE_COLUMNS = ("effort", "model", "billing")

_COST_SAMPLE_SELECT = (
    "SELECT timestamp, cost_usd, input_tokens, output_tokens, "
    "cache_read_tokens, cache_creation_tokens, caller, duration_ms, "
    "estimated, unmetered, provider, effort, model, billing FROM cost_samples "
)

#: Column order shared by the SELECT / INSERT statements and ``_task_to_row``.
_TASK_QUEUE_COLUMNS = (
    "task_id",
    "goal",
    "linked_question",
    "rationale",
    "created_at",
    "last_touched_at",
    "acceptance_criteria",
    "workspace_moment_id",
    "workspace_item_ids",
)

#: The subset of :data:`_TASK_QUEUE_COLUMNS` that ``_TASK_QUEUE_DDL`` declares
#: ``NOT NULL`` — the fields :class:`~maestro.task_queue.QueuedTask` types as a
#: plain ``str``. SQLite cannot ``ADD COLUMN ... NOT NULL`` without a default,
#: and adding one of these as nullable TEXT would hand ``_row_to_task`` a
#: ``None`` for a ``str``-typed field, so the additive back-fill skips them and
#: the post-back-fill guard reports them as unrepairable instead.
_TASK_QUEUE_REQUIRED_COLUMNS = frozenset(
    {"task_id", "goal", "created_at", "last_touched_at"},
)


def _task_to_row(task: QueuedTask) -> tuple[object, ...]:
    """Serialize a :class:`QueuedTask` into a row tuple (sans ``position``)."""
    crit = (
        json.dumps(task.acceptance_criteria)
        if task.acceptance_criteria is not None
        else None
    )
    # ``workspace_item_ids`` mirrors ``acceptance_criteria``: a JSON list or
    # NULL. ``[]`` serialises to ``"[]"`` (not NULL) so the ``None``-vs-``[]``
    # citation distinction (proposal 130) survives the round-trip.
    item_ids = (
        json.dumps(task.workspace_item_ids)
        if task.workspace_item_ids is not None
        else None
    )
    return (
        task.task_id,
        task.goal,
        task.linked_question,
        task.rationale,
        task.created_at,
        task.last_touched_at,
        crit,
        task.workspace_moment_id,
        item_ids,
    )


def _row_to_task(row: sqlite3.Row) -> QueuedTask:
    """Deserialize a stored row back into a :class:`QueuedTask`."""
    raw_crit = row["acceptance_criteria"]
    criteria = json.loads(raw_crit) if raw_crit is not None else None
    raw_item_ids = row["workspace_item_ids"]
    item_ids = json.loads(raw_item_ids) if raw_item_ids is not None else None
    return QueuedTask(
        task_id=row["task_id"],
        goal=row["goal"],
        linked_question=row["linked_question"],
        rationale=row["rationale"],
        created_at=row["created_at"],
        last_touched_at=row["last_touched_at"],
        acceptance_criteria=criteria,
        workspace_moment_id=row["workspace_moment_id"],
        workspace_item_ids=item_ids,
    )


def _dropped_directive_to_row(entry: DroppedDirective) -> tuple[object, ...]:
    """Serialize a :class:`DroppedDirective` into a row tuple (sans ``id``).

    ``pr_numbers`` and ``shas`` are JSON-encoded lists, mirroring the JSONL row
    the legacy ledger writes, so the store round-trips the whole entry.
    """
    return (
        entry.text,
        entry.source,
        json.dumps(list(entry.pr_numbers)),
        json.dumps(list(entry.shas)),
        entry.recorded_at,
    )


def _row_to_dropped_directive(row: sqlite3.Row) -> DroppedDirective:
    """Deserialize a stored row back into a :class:`DroppedDirective`."""
    return DroppedDirective(
        text=row["text"],
        source=row["source"],
        pr_numbers=tuple(json.loads(row["pr_numbers"])),
        shas=tuple(json.loads(row["shas"])),
        recorded_at=row["recorded_at"],
    )


def _settled_verdict_to_row(record: SettledVerdictRecord) -> tuple[object, ...]:
    """Serialize a :class:`SettledVerdictRecord` into a keyed-replace row tuple.

    Column order matches the ``settled_verdicts`` ``INSERT OR REPLACE``:
    ``(pr_number, settled, kind, settled_at_iteration, retracted_at_iteration,
    verdict_task_id)``. A settle record fills ``settled_at_iteration`` and leaves
    ``retracted_at_iteration`` NULL; a retraction record does the reverse and
    clears ``kind`` / ``verdict_task_id`` — the same split
    :func:`~maestro.audit_checks_settlement._settled_record_to_row` writes to the JSONL row.
    """
    if record.settled:
        return (
            record.pr_number,
            1,
            record.kind,
            record.iteration,
            None,
            record.verdict_task_id,
        )
    return (record.pr_number, 0, "", None, record.iteration, None)


def _stand_down_row_to_insert(row: StandDownRow) -> tuple[object, ...]:
    """Serialize a :class:`StandDownRow` into an append row tuple.

    Column order matches the ``stand_down_verdicts`` ``INSERT`` (sans the
    auto-assigned ``id``): ``(pr, head_sha, recorded_at, released_at,
    branch_ahead_of_origin)``. ``branch_ahead_of_origin`` maps ``True`` / ``False``
    to ``1`` / ``0`` and ``None`` to NULL, so a legacy or mechanical verdict (no
    reachability probe) round-trips back to ``None`` on read.
    """
    return (
        row.pr,
        row.head_sha,
        row.recorded_at,
        row.released_at,
        None if row.branch_ahead_of_origin is None else int(row.branch_ahead_of_origin),
    )


def _row_to_stand_down_row(row: sqlite3.Row) -> StandDownRow:
    """Reconstruct a verdict :class:`StandDownRow` from a ``stand_down_verdicts`` row.

    Used by :meth:`StateStore.stand_down_verdict_rows`, whose query already
    filters to genuine verdict rows (``pr > 0``, non-NULL ``head_sha`` /
    ``recorded_at``), so ``pr`` / ``head_sha`` / ``recorded_at`` are never NULL
    here. ``branch_ahead_of_origin`` maps a stored ``1`` / ``0`` back to
    ``True`` / ``False`` and NULL to ``None``.
    """
    ahead = row["branch_ahead_of_origin"]
    return StandDownRow(
        pr=row["pr"],
        head_sha=row["head_sha"],
        recorded_at=row["recorded_at"],
        branch_ahead_of_origin=None if ahead is None else bool(ahead),
    )


def _cost_sample_to_row(sample: CostSample) -> tuple[object, ...]:
    """Serialize a :class:`CostSample` into a row tuple (sans ``id``).

    ``ts_epoch`` is derived once, at write time, from the ISO ``timestamp`` so
    windowed reads range-scan a numeric column rather than re-parsing every
    stored string. It goes through :func:`~maestro.cost_tracking._as_utc` so a
    naive stamp yields a UTC epoch rather than a host-local one, keeping this
    column in the same domain as the JSONL reader's cutoff. The two bool flags
    narrow to ``INTEGER`` 0/1.
    """
    return (
        sample.timestamp,
        _as_utc(sample.timestamp).timestamp(),
        sample.cost_usd,
        sample.input_tokens,
        sample.output_tokens,
        sample.cache_read_tokens,
        sample.cache_creation_tokens,
        sample.caller,
        sample.duration_ms,
        int(sample.estimated),
        int(sample.unmetered),
        sample.provider,
        sample.effort,
        sample.model,
        sample.billing,
    )


def _row_to_cost_sample(row: sqlite3.Row) -> CostSample:
    """Deserialize a stored row back into a :class:`CostSample`.

    The ``ts_epoch`` companion is a read-time index only, never surfaced — the
    reconstructed sample carries the verbatim ISO ``timestamp`` string, so
    downstream aggregation is byte-for-byte identical to the JSONL path.
    """
    return CostSample(
        timestamp=row["timestamp"],
        cost_usd=row["cost_usd"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_creation_tokens=row["cache_creation_tokens"],
        caller=row["caller"],
        duration_ms=row["duration_ms"],
        estimated=bool(row["estimated"]),
        unmetered=bool(row["unmetered"]),
        provider=row["provider"],
        effort=row["effort"],
        model=row["model"],
        # NULL (a legacy row) or an unknown value reads as ``subscription``.
        billing=(
            cast(CostBilling, row["billing"]) if row["billing"] in COST_BILLINGS
            else "subscription"
        ),
    )


def _remediation_tracker_rows(state: TrackerState) -> list[tuple[str, str]]:
    """Project a :class:`TrackerState` into sorted ``(pr_key, blob)`` rows.

    One row per PR key present in *any* of the four maps, whose blob carries
    exactly the facets present for that key — so an empty-but-present facet
    (a PR with skips but no outcomes) round-trips, and a facet added later
    lands in the blob rather than in a new column. Sorted for a deterministic
    on-disk order, matching :meth:`StateStore._write_pr_ledger_rows`.
    """
    keys = (
        set(state.outcomes)
        | set(state.items_remaining)
        | set(state.skips)
        | set(state.merge_block_skips)
    )
    rows: list[tuple[str, str]] = []
    for key in sorted(keys):
        blob: dict[str, object] = {}
        if key in state.outcomes:
            blob["outcomes"] = state.outcomes[key]
        if key in state.items_remaining:
            blob["items_remaining"] = state.items_remaining[key]
        if key in state.skips:
            blob["skips"] = state.skips[key]
        if key in state.merge_block_skips:
            blob["merge_block_skips"] = state.merge_block_skips[key]
        rows.append((key, json.dumps(blob)))
    return rows


@dataclass(frozen=True)
class StateMigrationSummary:
    """The provenance-stamped result of one :meth:`StateStore.migrate_from_legacy`.

    ``seeded`` maps each store the converter seeded *this run* to the number of
    legacy rows it carried over; ``already_seeded`` names the stores whose
    durable seed marker was already stamped (by a prior eager run or an earlier
    lazy first-access), so the pass left them untouched. :meth:`render` folds
    both into one log line (proposal 106).
    """

    seeded: dict[str, int]
    already_seeded: list[str]

    def render(self) -> str:
        """Return a single-line, deterministically-ordered migration summary."""
        if self.seeded:
            seeded_desc = ", ".join(
                f"{name}={count}"
                for name, count in sorted(self.seeded.items())
            )
        else:
            seeded_desc = "none"
        already_desc = ", ".join(sorted(self.already_seeded)) or "none"
        total_rows = sum(self.seeded.values())
        return (
            f"seeded {len(self.seeded)} store(s), {total_rows} row(s) "
            f"[{seeded_desc}]; already-seeded {len(self.already_seeded)} "
            f"store(s) [{already_desc}]"
        )


@dataclass(frozen=True)
class _MigrationEntry:
    """One store's slot in the eager migration registry (proposal 106).

    ``is_seeded`` is the store's durable-marker predicate (a bound
    ``StateStore.is_*_seeded`` method); ``seed`` is a zero-argument closure that
    loads the legacy source, seeds it against the shared connection, and returns
    the row count. The registry is walked by :meth:`StateStore.migrate_from_legacy`;
    the coverage-guard test cross-checks ``is_seeded.__name__`` against every
    ``is_*_seeded`` predicate on :class:`StateStore` so a future ledger fold that
    forgets to register here fails loudly.
    """

    store_name: str
    is_seeded: Callable[[], bool]
    seed: Callable[[], int]


class StateStore:
    """Owns one long-lived SQLite connection and the state schema.

    Construct from a DB path (a filesystem path or the literal ``":memory:"``)
    *or* an injected :class:`sqlite3.Connection` so tests never open a real file.
    The connection uses :class:`sqlite3.Row` so columns are addressable by name,
    and ``check_same_thread=False`` because the single connection is held for the
    process lifetime and the loop is single-threaded (no cross-thread guard is
    needed, and the default same-thread check would only manufacture a spurious
    error if the connection were ever touched from an executor).
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is not None:
            self._conn = connection
        elif db_path is not None:
            self._conn = sqlite3.connect(
                str(db_path), check_same_thread=False,
            )
        else:
            raise ValueError(
                "StateStore requires either a db_path or a connection",
            )
        self._conn.row_factory = sqlite3.Row
        # Install the busy timeout before the DDL so even schema creation waits
        # on a cross-connection lock instead of failing fast (see
        # :data:`_BUSY_TIMEOUT_MS`).
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._conn.execute(_TASK_QUEUE_DDL)
        # ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a
        # store whose ``task_queue`` predates a column keeps the old shape while
        # the SELECT/INSERT statements already name the new one. Back-fill every
        # column :data:`_TASK_QUEUE_COLUMNS` declares but the live table lacks —
        # derived from the declaration rather than a literal list, so a field
        # added to :class:`~maestro.task_queue.QueuedTask` is repaired without a
        # second hand-edit here. Guarded by a ``PRAGMA table_info`` read so
        # re-opening an already-migrated store is a no-op; each added column is
        # nullable TEXT (JSON-serialized for the list fields), matching
        # ``_TASK_QUEUE_DDL``. A missing ``NOT NULL`` column
        # (:data:`_TASK_QUEUE_REQUIRED_COLUMNS`) is deliberately *not* added:
        # SQLite rejects ``ADD COLUMN ... NOT NULL`` without a default, and
        # adding it as nullable would feed ``None`` into a ``str``-typed
        # :class:`~maestro.task_queue.QueuedTask` field — so it falls through to
        # the guard below as unrepairable.
        existing_task_queue_cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(task_queue)")
        }
        for col in _TASK_QUEUE_COLUMNS:
            if (
                col not in existing_task_queue_cols
                and col not in _TASK_QUEUE_REQUIRED_COLUMNS
            ):
                self._conn.execute(
                    f"ALTER TABLE task_queue ADD COLUMN {col} TEXT",
                )
        # Startup is where the schema invariant promises the repair happened, so
        # a column the back-fill could not add — a ``NOT NULL`` column it skips,
        # or one an ``ALTER`` did not take — fails here, named, instead of
        # surfacing mid-loop as a ``sqlite3.OperationalError`` on an unrelated
        # queue statement.
        repaired_task_queue_cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(task_queue)")
        }
        missing_task_queue_cols = [
            col
            for col in _TASK_QUEUE_COLUMNS
            if col not in repaired_task_queue_cols
        ]
        if missing_task_queue_cols:
            raise StateStoreSchemaError(
                "task_queue table is missing declared column(s) "
                f"{', '.join(missing_task_queue_cols)} after the additive "
                "back-fill; the state.db schema cannot be repaired in place",
            )
        self._conn.execute(_METADATA_DDL)
        self._conn.execute(_COST_SAMPLES_DDL)
        # Same additive back-fill shape as ``task_queue`` above: a store whose
        # ``cost_samples`` predates the proposal-140 attribution columns gains
        # them as nullable TEXT (legacy rows read back ``None``), then a
        # re-read fails loud, named, if any declared column is still missing.
        existing_cost_cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(cost_samples)")
        }
        for col in _COST_SAMPLE_ADDITIVE_COLUMNS:
            if col not in existing_cost_cols:
                self._conn.execute(
                    f"ALTER TABLE cost_samples ADD COLUMN {col} TEXT",
                )
        repaired_cost_cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(cost_samples)")
        }
        missing_cost_cols = [
            col for col in _COST_SAMPLE_COLUMNS if col not in repaired_cost_cols
        ]
        if missing_cost_cols:
            raise StateStoreSchemaError(
                "cost_samples table is missing declared column(s) "
                f"{', '.join(missing_cost_cols)} after the additive "
                "back-fill; the state.db schema cannot be repaired in place",
            )
        self._conn.execute(_COST_SAMPLES_INDEX_DDL)
        self._conn.execute(_JOURNAL_SEARCH_DDL)
        self._conn.execute(_DEED_LEDGER_DDL)
        self._conn.execute(_LEVER_ATTEMPTS_DDL)
        self._conn.execute(_AUDIT_HISTORY_DDL)
        self._conn.execute(_REJECTION_HISTORY_DDL)
        self._conn.execute(_QUESTION_RESOLUTIONS_DDL)
        self._conn.execute(_GATE_ACTIVATIONS_DDL)
        self._conn.execute(_PREDICTIONS_DDL)
        self._conn.execute(_PREMISE_OBSERVATIONS_DDL)
        self._conn.execute(_OWNED_BACKLOG_DDL)
        self._conn.execute(_PR_PROVENANCE_DDL)
        self._conn.execute(_OBLIGATION_FORK_DDL)
        self._conn.execute(_FETCH_TRUTH_DDL)
        self._conn.execute(_PREFLIGHT_PASSES_DDL)
        self._conn.execute(_DROPPED_DIRECTIVES_DDL)
        self._conn.execute(_SETTLED_VERDICTS_DDL)
        self._conn.execute(_STAND_DOWN_VERDICTS_DDL)
        self._conn.execute(_UNCONFIRMED_LANDING_DDL)
        self._conn.execute(_ORPHANED_CURE_DDL)
        self._conn.execute(_IMPACT_REVIEW_SURFACED_DDL)
        self._conn.execute(_MERGEABLE_SERVICED_DDL)
        self._conn.execute(_SETTLEMENT_RECONCILIATIONS_DDL)
        self._conn.execute(_VERSION_ARCHIVE_DDL)
        self._conn.execute(_PR_LEDGER_DDL)
        self._conn.execute(_REMEDIATION_TRACKER_DDL)
        self._conn.execute(_ESCALATION_STANDDOWNS_DDL)
        self._conn.execute(_CONSCIOUS_EPISODES_DDL)
        self._conn.commit()

    # -- metadata ---------------------------------------------------------

    def is_task_queue_seeded(self) -> bool:
        """Return whether the one-time seed marker has been stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_TASK_QUEUE_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def mark_task_queue_seeded(self) -> None:
        """Durably stamp the one-time seed marker (idempotent)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_TASK_QUEUE_SEEDED_KEY,),
            )

    # -- task queue -------------------------------------------------------

    def load_task_queue(self) -> list[QueuedTask]:
        """Return every queued task in ``position`` (FIFO) order."""
        # Derived from the declaration rather than a hand-written list, so a
        # column added to ``_TASK_QUEUE_COLUMNS`` is selected back as well as
        # inserted and back-filled.
        columns = ", ".join(_TASK_QUEUE_COLUMNS)
        cur = self._conn.execute(
            f"SELECT {columns} FROM task_queue ORDER BY position ASC",
        )
        return [_row_to_task(row) for row in cur.fetchall()]

    def _write_task_queue_rows(self, items: list[QueuedTask]) -> None:
        """Rewrite the whole queue table; caller owns the transaction."""
        placeholders = ", ".join("?" for _ in _TASK_QUEUE_COLUMNS)
        columns = ", ".join(("position", *_TASK_QUEUE_COLUMNS))
        insert = (
            f"INSERT INTO task_queue ({columns}) "
            f"VALUES (?, {placeholders})"
        )
        rows = [
            (position, *_task_to_row(task))
            for position, task in enumerate(items)
        ]
        self._conn.execute("DELETE FROM task_queue")
        self._conn.executemany(insert, rows)

    def replace_task_queue(self, items: list[QueuedTask]) -> None:
        """Atomically replace the whole queue, numbering positions 0..n-1."""
        with self._conn:
            self._write_task_queue_rows(items)

    def mutate_task_queue(self, fn: Callable[[list[QueuedTask]], T]) -> T:
        """Apply *fn* to the live queue atomically, then persist it.

        The whole load → *fn* → whole-queue rewrite runs inside one ``BEGIN
        IMMEDIATE`` transaction, so the read-modify-write is serialised against
        every other ``state.db`` connection — matching the
        :func:`~maestro.fileutil.file_lock` span the JSON backend's
        :meth:`~maestro.task_queue.TaskQueue.mutate` holds, and the one
        :meth:`pop_task_queue_head` already takes. Without it a curator that
        loaded the queue, ran a multi-minute SDK call, and then replaced the
        whole queue would erase any head popped in between, resurrecting an
        already-dispatched task. *fn* raising rolls the transaction back and
        re-raises, leaving the queue untouched.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            items = self.load_task_queue()
            result = fn(items)
            self._write_task_queue_rows(items)
            return result

    def pop_task_queue_head(self) -> QueuedTask | None:
        """Remove and return the lowest-``position`` task, or ``None``.

        The SELECT of the head row and the DELETE that consumes it run inside
        one ``BEGIN IMMEDIATE`` transaction (the same write-lock-up-front guard
        :meth:`mutate_pr_ledger` uses), so the pair is serialised across every
        ``state.db`` connection — without it two connections could both fetch
        the same head and both delete it, dispatching the same queued task
        twice; the loser now blocks on ``busy_timeout`` until this commit.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            columns = ", ".join(("position", *_TASK_QUEUE_COLUMNS))
            cur = self._conn.execute(
                f"SELECT {columns} FROM task_queue "
                "ORDER BY position ASC LIMIT 1",
            )
            row = cur.fetchone()
            if row is None:
                return None
            self._conn.execute(
                "DELETE FROM task_queue WHERE position = ?", (row["position"],),
            )
        return _row_to_task(row)

    # -- cost samples -----------------------------------------------------

    def append_cost_sample(self, sample: CostSample) -> None:
        """Append one cost sample, auto-assigning its FIFO ``id``."""
        placeholders = ", ".join("?" for _ in _COST_SAMPLE_COLUMNS)
        columns = ", ".join(_COST_SAMPLE_COLUMNS)
        with self._conn:
            self._conn.execute(
                f"INSERT INTO cost_samples ({columns}) VALUES ({placeholders})",
                _cost_sample_to_row(sample),
            )

    def load_cost_samples_since(self, cutoff_epoch: float) -> list[CostSample]:
        """Return samples whose ``ts_epoch`` is at or after *cutoff_epoch*.

        Ordered ``(ts_epoch, id)`` ascending (oldest-first) to match the JSONL
        reader's post-reverse order: samples are appended in wall-clock order so
        ``ts_epoch`` is monotonic with the ``id`` rowid, making this identical to
        an ``id``-ascending sort, with ``id`` only breaking exact-epoch ties
        deterministically. Leading with ``ts_epoch`` (not ``id``) is load-bearing
        for performance — it lets ``idx_cost_samples_ts_epoch`` serve both the
        range seek and the order, so the read touches only the in-window rows
        instead of scanning the whole append-only table on the throttle hot path.
        The ``>=`` boundary mirrors ``_iter_samples_in_window`` (which keeps a
        sample iff its epoch is not strictly below the cutoff).
        """
        cur = self._conn.execute(
            _COST_SAMPLE_SELECT + "WHERE ts_epoch >= ? ORDER BY ts_epoch ASC, id ASC",
            (cutoff_epoch,),
        )
        return [_row_to_cost_sample(row) for row in cur.fetchall()]

    def move_cost_samples_to_cold(
        self, *, cutoff_epoch: float, callers: Iterable[str], cold_path: Path,
    ) -> int:
        """Move fenced leak rows to *cold_path*; return how many moved.

        Inside one ``BEGIN IMMEDIATE``: select rows at or before *cutoff_epoch*
        whose caller is in *callers* or whose provider is NULL, append each as
        an ``asdict(CostSample)`` JSON line to *cold_path*, then delete them.
        Idempotent — a second call finds nothing and returns 0.
        """
        names = sorted(callers)
        marks = ", ".join("?" for _ in names)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT id, " + _COST_SAMPLE_SELECT[len("SELECT "):]
                + f"WHERE ts_epoch <= ? AND (caller IN ({marks}) "
                "OR provider IS NULL) ORDER BY id",
                (cutoff_epoch, *names),
            ).fetchall()
            if not rows:
                return 0
            with open(cold_path, "a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(asdict(_row_to_cost_sample(row))) + "\n")
            self._conn.executemany(
                "DELETE FROM cost_samples WHERE id = ?",
                [(row["id"],) for row in rows],
            )
        return len(rows)

    def recent_cost_samples(self, limit: int) -> list[CostSample]:
        """Return the most recent *limit* samples in oldest-first order.

        Selects the newest rows via ``id`` descending then reverses, so the
        result matches a tail read of the JSONL file (the last *limit* lines,
        oldest-first). A non-positive *limit* returns an empty list.
        """
        if limit <= 0:
            return []
        cur = self._conn.execute(
            _COST_SAMPLE_SELECT + "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        samples = [_row_to_cost_sample(row) for row in cur.fetchall()]
        samples.reverse()
        return samples

    def max_cost_sample_id(self) -> int | None:
        """Return the highest cost-sample ``id``, or ``None`` when empty.

        Strictly monotonic across the append-only lifetime, so it serves as the
        TTL-cache version key (the SQLite analogue of the JSONL file mtime).
        """
        cur = self._conn.execute("SELECT MAX(id) AS max_id FROM cost_samples")
        row = cur.fetchone()
        return row["max_id"] if row is not None else None

    # -- journal search index ---------------------------------------------

    def _journal_search_watermark(self) -> int | None:
        """Return the ``journal_search`` watermark, or ``None`` to force a rebuild.

        ``None`` means "no usable watermark" — the key is absent (a fresh DB or
        a flag flip) or its value does not parse as a non-negative integer (a
        tampered / out-of-band-edited row). Either way the reconciler rebuilds
        the whole table from the corpus rather than trusting a count it cannot
        stand behind. ``int()`` is the arbiter (mirroring
        :meth:`_remediation_tracker_version`) so a garbage stamp degrades to a
        rebuild instead of raising mid-search.
        """
        row = self._conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?",
            (_JOURNAL_SEARCH_INDEXED_COUNT_KEY,),
        ).fetchone()
        if row is None:
            return None
        try:
            value = int(row["value"])
        except (ValueError, TypeError):
            return None
        return value if value >= 0 else None

    def _reconcile_journal_search(self, corpus: Sequence[str]) -> None:
        """Bring ``journal_search`` in line with *corpus* (caller holds the lock).

        Called inside :meth:`rank_journal_search`'s ``BEGIN IMMEDIATE``
        transaction. Three cases, keyed on the watermark:

        - **current** (watermark == ``len(corpus)``): the index already covers
          the corpus exactly — return without a single write, so a repeat search
          over an unchanged journal re-indexes nothing.
        - **behind** (watermark < ``len(corpus)``): index only the appended tail
          ``corpus[watermark:]`` at its corpus positions (``rowid`` == index),
          the whole point of the persistent index.
        - **ahead / unusable** (watermark ``None`` or > ``len(corpus)``): the
          corpus shrank or was edited out of append-only order, or the watermark
          is absent/garbage — rebuild the whole table from ``corpus``.

        Each doc is stored as ``" ".join(tokenize(doc))`` exactly as the
        ephemeral ranker inserts, and the watermark is upserted to
        ``len(corpus)`` in the same transaction, so the count and the table stay
        in lockstep and bm25's corpus statistics span the same N documents the
        ephemeral ranker sees.
        """
        watermark = self._journal_search_watermark()
        if watermark is not None and watermark == len(corpus):
            return
        if watermark is None or watermark > len(corpus):
            self._conn.execute("DELETE FROM journal_search")
            start = 0
        else:
            start = watermark
        if start < len(corpus):
            self._conn.executemany(
                "INSERT INTO journal_search (rowid, content) VALUES (?, ?)",
                [(i, " ".join(tokenize(corpus[i]))) for i in range(start, len(corpus))],
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO store_metadata (key, value) VALUES (?, ?)",
            (_JOURNAL_SEARCH_INDEXED_COUNT_KEY, str(len(corpus))),
        )

    def rank_journal_search(self, corpus: Sequence[str], query: str) -> list[int]:
        """Rank ``corpus`` by BM25 via the persistent ``journal_search`` index.

        One ``BEGIN IMMEDIATE`` transaction (the same write-lock-up-front guard
        the seed-once methods use, so concurrent connections over the shared
        ``state.db`` serialise rather than double-index) reconciles the index to
        ``corpus`` (:meth:`_reconcile_journal_search`), then runs the same
        ``bm25()`` SELECT the ephemeral ranker uses. Because the reconciler
        leaves exactly ``rowid`` ``0..len(corpus)-1`` populated, the MATCH is
        already restricted to the current corpus. Returns indices into ``corpus``
        sorted by descending BM25 with a stable ``rowid`` tie-break, and ``[]``
        for an empty/whitespace/punctuation-only query, an empty corpus, or a
        corpus sharing no query token — the exact contract
        :func:`~maestro.search.bm25_rank` honours.
        """
        query_tokens = tokenize(query)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._reconcile_journal_search(corpus)
            if not corpus or not query_tokens:
                return []
            match_query = " OR ".join(f'"{t}"' for t in query_tokens)
            rows = self._conn.execute(
                "SELECT rowid FROM journal_search "
                "WHERE journal_search MATCH ? "
                "ORDER BY bm25(journal_search), rowid",
                (match_query,),
            ).fetchall()
        return [int(r[0]) for r in rows]

    # -- deed ledger ------------------------------------------------------

    def is_deed_ledger_seeded(self) -> bool:
        """Return whether the one-time deed-ledger seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_DEED_LEDGER_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_deed(self, deed: Deed) -> None:
        """Append one deed row, auto-assigning its FIFO ``id``."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO deeds (deed_type, pr_number, detail, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (deed.deed_type, deed.pr_number, deed.detail, deed.timestamp),
            )

    def seed_deeds_once(self, deeds: list[Deed]) -> None:
        """Seed the deeds table from *deeds* exactly once, atomically.

        Guards the marker read, the empty-table check, the bulk insert, and the
        marker stamp inside one ``BEGIN IMMEDIATE`` transaction. Several
        connections share the one ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`),
        so two of them racing their first seed would each pass a lock-free
        marker / emptiness check and both ``INSERT`` — doubling the all-time
        deed count, since the deed seed only appends (unlike the task queue's
        idempotent DELETE-then-insert ``replace_task_queue``). Acquiring the
        write lock up front with ``BEGIN IMMEDIATE`` (rather than the default
        deferred begin, which takes it only at the first write, after the
        lock-free checks have already decided to seed) serializes them: the
        loser blocks on ``busy_timeout``, then re-reads the now-stamped marker
        inside its own transaction and skips. The insert stays guarded on an
        empty table so a store populated by a prior run that crashed before
        stamping the marker is never doubled either.

        *deeds* is oldest-first (see
        :func:`~maestro.deed_ledger.load_all_deeds`), so the ``AUTOINCREMENT``
        ``id`` reproduces the append order and a later ``ORDER BY id DESC`` read
        is newest-first, matching the JSONL reverse walk.
        """
        rows = [
            (d.deed_type, d.pr_number, d.detail, d.timestamp) for d in deeds
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_DEED_LEDGER_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM deeds",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO deeds "
                    "(deed_type, pr_number, detail, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_DEED_LEDGER_SEEDED_KEY,),
            )

    def count_deeds_since(self, since: str | None = None) -> int:
        """Count recorded deeds, optionally windowed to *since* onward.

        *since* is an ISO-8601 UTC timestamp; only deeds stamped at or after it
        are counted (``timestamp >= ?``). Deeds are wall-clock-appended, so
        lexical string order equals chronological order and this matches the
        JSONL reader's ``since`` comparison.
        """
        if since is None:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM deeds")
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS n FROM deeds WHERE timestamp >= ?",
                (since,),
            )
        row = cur.fetchone()
        return int(row["n"]) if row is not None else 0

    def recent_deeds(self, limit: int) -> list[Deed]:
        """Return the most recent *limit* deeds, newest-first.

        Selects the highest-``id`` rows (``ORDER BY id DESC``), matching the
        JSONL reverse walk. A non-positive *limit* returns an empty list — SQLite
        treats a negative ``LIMIT`` as unbounded, so the guard is load-bearing.
        """
        if limit <= 0:
            return []
        cur = self._conn.execute(
            "SELECT deed_type, pr_number, detail, timestamp FROM deeds "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            Deed(
                deed_type=row["deed_type"],
                pr_number=row["pr_number"],
                detail=row["detail"],
                timestamp=row["timestamp"],
            )
            for row in cur.fetchall()
        ]

    # -- lever-attempts ledger --------------------------------------------

    def is_lever_attempts_seeded(self) -> bool:
        """Return whether the one-time lever-attempts seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_LEVER_ATTEMPTS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_lever_attempt(self, attempt: LeverAttempt) -> None:
        """Append one lever-attempt row, auto-assigning its FIFO ``id``.

        ``args`` is JSON-encoded and the two booleans store as ``INTEGER`` 0/1,
        matching :data:`_LEVER_ATTEMPTS_DDL`.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO lever_attempts (lever, args, verified, result) "
                "VALUES (?, ?, ?, ?)",
                (
                    attempt.lever,
                    json.dumps(attempt.args),
                    int(attempt.verified),
                    int(attempt.result),
                ),
            )

    def load_lever_attempts(self) -> list[LeverAttempt]:
        """Return every lever-attempt row oldest-first (``ORDER BY id ASC``).

        The seed / selector read-back: the ``AUTOINCREMENT`` ``id`` preserves
        append order, so an ascending read reproduces the JSONL forward
        (oldest-first) order. ``args`` is decoded from its JSON storage and the
        two flags are coerced back from their ``INTEGER`` 0/1 storage to ``bool``.
        """
        cur = self._conn.execute(
            "SELECT lever, args, verified, result FROM lever_attempts "
            "ORDER BY id ASC",
        )
        return [
            LeverAttempt(
                lever=row["lever"],
                args=[str(a) for a in json.loads(row["args"])],
                verified=bool(row["verified"]),
                result=bool(row["result"]),
            )
            for row in cur.fetchall()
        ]

    def seed_lever_attempts_once(self, attempts: list[LeverAttempt]) -> None:
        """Seed the lever-attempts table from *attempts* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_deeds_once`: the ledger only appends (no idempotent replace),
        so two connections racing their first seed over the shared ``state.db``
        (see :data:`_BUSY_TIMEOUT_MS`) would each pass a lock-free marker /
        emptiness check and both ``INSERT``, doubling the distinct-``True`` count
        the backlog sensor tracks. Acquiring the write lock up front with ``BEGIN
        IMMEDIATE`` serializes them: the loser blocks on ``busy_timeout``, then
        re-reads the now-stamped marker inside its own transaction and skips. The
        insert stays guarded on an empty table so a store populated by a prior run
        that crashed before stamping the marker is never doubled either.

        *attempts* is oldest-first (see
        :func:`~maestro.lever_capture.load_all_lever_attempts`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later ``ORDER
        BY id ASC`` read matches the JSONL forward order.
        """
        rows = [
            (a.lever, json.dumps(a.args), int(a.verified), int(a.result))
            for a in attempts
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_LEVER_ATTEMPTS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM lever_attempts",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO lever_attempts "
                    "(lever, args, verified, result) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_LEVER_ATTEMPTS_SEEDED_KEY,),
            )

    # -- audit-history ledger ---------------------------------------------

    def is_audit_history_seeded(self) -> bool:
        """Return whether the one-time audit-history seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_AUDIT_HISTORY_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_audit_result(self, result: AuditResult) -> None:
        """Append one audit-result row, auto-assigning its FIFO ``id``.

        The whole result stores as a single ``json.dumps(asdict(result))`` blob
        in ``data``, matching :data:`_AUDIT_HISTORY_DDL` — so additive
        ``AuditResult`` fields (and its nested ``findings`` list) never force a
        table migration.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO audit_history (data) VALUES (?)",
                (json.dumps(asdict(result)),),
            )

    def load_audit_results(self) -> list[AuditResult]:
        """Return every audit-result row oldest-first (``ORDER BY id ASC``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending
        read reproduces the JSONL forward (oldest-first) order. Each row's
        ``data`` blob is decoded and hydrated through
        :func:`~maestro.memory._parse_audit_dict` — the same core the JSONL
        reader uses — so a blob written by a newer schema (extra keys) or an
        older one round-trips identically, and any structurally-corrupt blob is
        dropped (not raised) exactly as the JSONL path drops it.
        """
        cur = self._conn.execute(
            "SELECT data FROM audit_history ORDER BY id ASC",
        )
        results: list[AuditResult] = []
        for row in cur.fetchall():
            result = _parse_audit_dict(json.loads(row["data"]))
            if result is not None:
                results.append(result)
        return results

    def seed_audit_history_once(self, results: list[AuditResult]) -> None:
        """Seed the audit-history table from *results* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_lever_attempts_once`: the ledger only appends (no idempotent
        replace), so two connections racing their first seed over the shared
        ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each pass a lock-free
        marker / emptiness check and both ``INSERT``, doubling the persisted
        audit history. Acquiring the write lock up front with ``BEGIN
        IMMEDIATE`` serializes them: the loser blocks on ``busy_timeout``, then
        re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a store populated by a
        prior run that crashed before stamping the marker is never doubled
        either.

        *results* is oldest-first (see
        :func:`~maestro.memory.load_all_audit_history`), so the ``AUTOINCREMENT``
        ``id`` reproduces the append order and a later ``ORDER BY id ASC`` read
        matches the JSONL forward order.
        """
        rows = [(json.dumps(asdict(r)),) for r in results]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_AUDIT_HISTORY_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM audit_history",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO audit_history (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_AUDIT_HISTORY_SEEDED_KEY,),
            )

    # -- rejection-history ledger -----------------------------------------

    def is_rejection_history_seeded(self) -> bool:
        """Return whether the one-time rejection-history seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_REJECTION_HISTORY_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_rejection(self, record: RejectionRecord) -> None:
        """Append one rejection row, auto-assigning its FIFO ``id``.

        The whole record stores as a single ``json.dumps(asdict(record))`` blob
        in ``data``, matching :data:`_REJECTION_HISTORY_DDL` — so additive
        ``RejectionRecord`` fields never force a table migration.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO rejection_history (data) VALUES (?)",
                (json.dumps(asdict(record)),),
            )

    def load_rejections(self) -> list[RejectionRecord]:
        """Return every rejection row oldest-first (``ORDER BY id ASC``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending
        read reproduces the JSONL forward (oldest-first) order. Each row's
        ``data`` blob is decoded and hydrated through
        :func:`~maestro.memory._parse_rejection_dict` — the same core the JSONL
        reader uses — so a blob written by a newer schema (extra keys) or a
        legacy ``priority`` string round-trips identically, and any
        structurally-corrupt blob is dropped (not raised) exactly as the JSONL
        path drops it.
        """
        cur = self._conn.execute(
            "SELECT data FROM rejection_history ORDER BY id ASC",
        )
        records: list[RejectionRecord] = []
        for row in cur.fetchall():
            record = _parse_rejection_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        return records

    def seed_rejection_history_once(
        self, records: list[RejectionRecord],
    ) -> None:
        """Seed the rejection-history table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_audit_history_once`: the ledger only appends (no idempotent
        replace), so two connections racing their first seed over the shared
        ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each pass a lock-free
        marker / emptiness check and both ``INSERT``, doubling the persisted
        rejection history. Acquiring the write lock up front with ``BEGIN
        IMMEDIATE`` serializes them: the loser blocks on ``busy_timeout``, then
        re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a store populated by a
        prior run that crashed before stamping the marker is never doubled
        either.

        *records* is oldest-first (see
        :func:`~maestro.memory.load_all_rejection_history`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id ASC`` read matches the JSONL forward order.
        """
        rows = [(json.dumps(asdict(r)),) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_REJECTION_HISTORY_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM rejection_history",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO rejection_history (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_REJECTION_HISTORY_SEEDED_KEY,),
            )

    # -- version-archive ledger -------------------------------------------

    def is_version_archive_seeded(self) -> bool:
        """Return whether the one-time version-archive seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_VERSION_ARCHIVE_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_version(self, record: VersionRecord) -> None:
        """Append one merged-plan row, auto-assigning its FIFO ``id``.

        The whole record stores as a single ``json.dumps(asdict(record))`` blob
        in ``data``, matching :data:`_VERSION_ARCHIVE_DDL` — so additive
        ``VersionRecord`` fields never force a table migration.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO version_archive (data) VALUES (?)",
                (json.dumps(asdict(record)),),
            )

    def load_versions(self) -> list[VersionRecord]:
        """Return every version row oldest-first (``ORDER BY id ASC``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending
        read reproduces the JSONL forward (oldest-first) order. Each row's
        ``data`` blob is decoded and hydrated through
        :meth:`~maestro.memory.Memory._parse_version_dict` — the same core the
        JSONL reader uses — so a blob written by a newer schema (extra keys)
        round-trips through the known fields, and any structurally-corrupt blob
        is dropped (not raised) exactly as the JSONL path drops it.
        """
        cur = self._conn.execute(
            "SELECT data FROM version_archive ORDER BY id ASC",
        )
        records: list[VersionRecord] = []
        for row in cur.fetchall():
            record = Memory._parse_version_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        return records

    def seed_version_archive_once(self, records: list[VersionRecord]) -> None:
        """Seed the version-archive table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_rejection_history_once`: the ledger only appends (no
        idempotent replace), so two connections racing their first seed over the
        shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each pass a
        lock-free marker / emptiness check and both ``INSERT``, doubling the
        persisted archive. Acquiring the write lock up front with ``BEGIN
        IMMEDIATE`` serialises them: the loser blocks on ``busy_timeout``, then
        re-reads the now-stamped marker inside its own transaction and skips. The
        insert stays guarded on an empty table so a store populated by a prior
        run that crashed before stamping the marker is never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.memory.load_all_versions`), so the ``AUTOINCREMENT`` ``id``
        reproduces the append order and a later ``ORDER BY id ASC`` read matches
        the JSONL forward order.
        """
        rows = [(json.dumps(asdict(r)),) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_VERSION_ARCHIVE_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM version_archive",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO version_archive (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_VERSION_ARCHIVE_SEEDED_KEY,),
            )

    # -- per-PR remediation ledger ----------------------------------------

    def is_pr_ledger_seeded(self) -> bool:
        """Return whether the one-time PR-ledger seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_PR_LEDGER_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def load_pr_ledger(self) -> dict[int, PRLedgerEntry]:
        """Return the whole ``{pr_number: entry}`` map from the keyed table.

        Each row's ``data`` blob is decoded and hydrated through the tolerant
        :func:`~maestro.pr_ledger._entry_from_raw` — the same core the JSON
        adapter uses — so a structurally-corrupt blob is dropped (not raised)
        exactly as the JSON path drops it, and a blob written by a newer schema
        round-trips through the known fields.
        """
        cur = self._conn.execute("SELECT data FROM pr_ledger")
        entries: dict[int, PRLedgerEntry] = {}
        for row in cur.fetchall():
            entry = _entry_from_raw(json.loads(row["data"]))
            if entry is not None:
                entries[entry.pr_number] = entry
        return entries

    def _write_pr_ledger_rows(self, entries: list[PRLedgerEntry]) -> None:
        """Rewrite the whole ``pr_ledger`` table; caller owns the transaction.

        A ``DELETE`` + ``executemany`` INSERT, mirroring
        :meth:`replace_task_queue`: the keyed ``{pr_number: entry}`` map is
        rewritten whole, so an entry dropped by
        :meth:`~maestro.pr_ledger.PRLedger.prune` /
        :meth:`~maestro.pr_ledger.PRLedger.evict_settled` leaves no orphan row.
        Each entry stores as a single ``json.dumps(asdict(entry))`` blob keyed by
        ``pr_number``, sorted for a deterministic on-disk order.
        """
        rows = [
            (entry.pr_number, json.dumps(asdict(entry)))
            for entry in sorted(entries, key=lambda e: e.pr_number)
        ]
        self._conn.execute("DELETE FROM pr_ledger")
        self._conn.executemany(
            "INSERT INTO pr_ledger (pr_number, data) VALUES (?, ?)",
            rows,
        )

    def replace_pr_ledger(self, entries: list[PRLedgerEntry]) -> None:
        """Atomically replace the whole ledger with *entries* (one transaction)."""
        with self._conn:
            self._write_pr_ledger_rows(entries)

    def mutate_pr_ledger(
        self, fn: Callable[[dict[int, PRLedgerEntry]], T],
    ) -> T:
        """Apply *fn* to the ledger map atomically; persist iff *fn* changed it.

        The whole load → *fn* → keyed-replace runs inside one ``BEGIN
        IMMEDIATE`` transaction, so the read-modify-write is serialised against
        every other ``state.db`` connection (a concurrent writer blocks on
        ``busy_timeout`` until this commit) — matching the
        :func:`~maestro.fileutil.file_lock` span the JSON store's
        :meth:`~maestro.pr_ledger.JsonPRLedger.mutate` holds across its RMW,
        so an interleaved writer can never make this connection persist a
        stale map. The change guard preserves the "quiet no-op writes
        nothing" contract: an unchanged map runs no rewrite, and the empty
        transaction commits without touching the table. *fn* raising rolls
        the transaction back and re-raises.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            entries = self.load_pr_ledger()
            before = entries_snapshot(entries)
            result = fn(entries)
            if entries_snapshot(entries) != before:
                self._write_pr_ledger_rows(list(entries.values()))
            return result

    def seed_pr_ledger_once(self, entries: list[PRLedgerEntry]) -> None:
        """Seed the pr_ledger table from *entries* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_settled_verdicts_once`: two connections racing their first
        seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would
        each pass a lock-free marker / emptiness check and both write. Acquiring
        the write lock up front serialises them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store carrying live budget/latch state is never overwritten by the frozen
        legacy JSON on a re-seed — even though the per-key ``INSERT OR REPLACE``
        is itself idempotent. *entries* is ``pr_number``-sorted (see
        :func:`~maestro.pr_ledger.load_all_pr_ledger_entries`).
        """
        rows = [
            (entry.pr_number, json.dumps(asdict(entry))) for entry in entries
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_PR_LEDGER_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM pr_ledger",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO pr_ledger (pr_number, data) "
                    "VALUES (?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_PR_LEDGER_SEEDED_KEY,),
            )

    # -- per-PR remediation-repetition tracker -----------------------------

    def is_remediation_tracker_seeded(self) -> bool:
        """Return whether the one-time remediation-tracker seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_REMEDIATION_TRACKER_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def load_remediation_tracker(self) -> TrackerState:
        """Return the whole tracker state re-assembled from the keyed table.

        Each row's blob contributes its present facets back to the four maps,
        and the re-assembled wrapper is handed to the tolerant
        :func:`~maestro.remediation_tracker._parse_tracker_payload` — the same
        parse core the JSON adapter reads through — so a structurally-corrupt
        value is dropped (not raised) exactly as the JSON path drops it. A blob
        that is not a mapping contributes nothing.

        The version comes from ``store_metadata``: ``0`` when never persisted or
        seeded (fresh-tracker semantics — no conflated history, so the one-time
        #2796 merge-block reconciliation is not owed), and a present-but-
        unparseable value reads as
        :data:`~maestro.remediation_tracker._TRACKER_LEGACY_VERSION` through the
        shared parse core, matching how the JSON path reads a bogus stamp.
        """
        outcomes: dict[str, object] = {}
        items: dict[str, object] = {}
        skips: dict[str, object] = {}
        merge_block_skips: dict[str, object] = {}
        facets = (
            ("outcomes", outcomes),
            ("items_remaining", items),
            ("skips", skips),
            ("merge_block_skips", merge_block_skips),
        )
        cur = self._conn.execute(
            "SELECT pr_key, data FROM remediation_tracker",
        )
        for row in cur.fetchall():
            blob = json.loads(row["data"])
            if not isinstance(blob, dict):
                continue
            for name, target in facets:
                if name in blob:
                    target[row["pr_key"]] = blob[name]
        return _parse_tracker_payload(
            {
                "outcomes": outcomes,
                "items_remaining": items,
                "skips": skips,
                "merge_block_skips": merge_block_skips,
                "schema_version": self._remediation_tracker_version(),
            },
        )

    def _remediation_tracker_version(self) -> object:
        """Return the stored schema version for the parse core to validate.

        ``0`` when unstamped (fresh tracker); the parsed integer when the
        metadata value parses as one; otherwise the raw string, which the
        shared parse core's integer guard reads as the legacy version — the
        same verdict the JSON path reaches on a non-integer
        ``schema_version``. ``int()`` is the arbiter (not an ``isdigit``
        pre-check, which admits forms like ``"--5"`` or ``"²"`` that ``int()``
        then rejects), so a tampered stamp degrades rather than raises.
        """
        row = self._conn.execute(
            "SELECT value FROM store_metadata WHERE key = ?",
            (_REMEDIATION_TRACKER_VERSION_KEY,),
        ).fetchone()
        if row is None:
            return 0
        stored = str(row["value"])
        try:
            return int(stored)
        except ValueError:
            return stored

    def replace_remediation_tracker(self, state: TrackerState) -> None:
        """Atomically replace the whole tracker state (one transaction).

        A ``DELETE`` + sorted ``executemany`` INSERT plus the version upsert,
        mirroring :meth:`replace_pr_ledger` — and mirroring the JSON path's
        whole-file :func:`~maestro.fileutil.atomic_write`, so a PR dropped by
        :meth:`~maestro.remediation_tracker.RemediationTracker.evict_settled` /
        ``reset`` leaves no orphan row. The version is upserted in the same
        transaction so the state and its stamp can never disagree.
        """
        rows = _remediation_tracker_rows(state)
        with self._conn:
            self._conn.execute("DELETE FROM remediation_tracker")
            self._conn.executemany(
                "INSERT INTO remediation_tracker (pr_key, data) VALUES (?, ?)",
                rows,
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO store_metadata (key, value) "
                "VALUES (?, ?)",
                (_REMEDIATION_TRACKER_VERSION_KEY, str(state.schema_version)),
            )

    def seed_remediation_tracker_once(self, state: TrackerState) -> None:
        """Seed the tracker table from *state* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_pr_ledger_once`: two connections racing their first seed
        over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both write. Acquiring the
        write lock up front serialises them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store carrying live gate state is never overwritten by the frozen legacy
        JSON on a re-seed.

        The legacy file's ``schema_version`` is seeded alongside its rows, so a
        pre-v2 sidecar still carries its stamp across the flag flip and the
        one-time #2796 merge-block reconciliation fires exactly once on the new
        substrate rather than being silently skipped.
        """
        rows = _remediation_tracker_rows(state)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_REMEDIATION_TRACKER_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM remediation_tracker",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO remediation_tracker "
                    "(pr_key, data) VALUES (?, ?)",
                    rows,
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO store_metadata (key, value) "
                    "VALUES (?, ?)",
                    (
                        _REMEDIATION_TRACKER_VERSION_KEY,
                        str(state.schema_version),
                    ),
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_REMEDIATION_TRACKER_SEEDED_KEY,),
            )

    # -- escalation-standdown ledger ---------------------------------------

    def is_escalation_standdowns_seeded(self) -> bool:
        """Return whether the one-time escalation-standdown seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_ESCALATION_STANDDOWNS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def load_escalation_standdowns(self) -> dict[int, StanddownRecord]:
        """Return the whole ``{number: record}`` map from the keyed table.

        Each row's ``data`` blob is decoded and hydrated through the tolerant
        :func:`~maestro.escalation_bridge._parse_standdown_entry` — the same
        core the JSON adapter reads through — so a structurally-corrupt blob is
        dropped (not raised) exactly as the JSON path drops it, and a blob
        written by a newer schema round-trips through the known fields —
        including the ``claim`` a deliverer stamps (proposal 135), which this
        path would otherwise drop when rebuilding a fresh record literal.
        """
        cur = self._conn.execute(
            "SELECT number, data FROM escalation_standdowns",
        )
        standdowns: dict[int, StanddownRecord] = {}
        for row in cur.fetchall():
            entry = _parse_standdown_entry(
                str(row["number"]), json.loads(row["data"]),
            )
            if entry is None:
                continue
            number, record = entry
            standdowns[number] = record
        return standdowns

    def _write_escalation_standdown_rows(
        self, standdowns: dict[int, StanddownRecord],
    ) -> None:
        """Rewrite the whole ``escalation_standdowns`` table; caller owns the transaction.

        A ``DELETE`` + ``executemany`` INSERT, mirroring
        :meth:`_write_pr_ledger_rows`: the keyed ``{number: record}`` map is
        rewritten whole, so a record dropped by
        :func:`~maestro.escalation_bridge.clear_standdown` /
        :func:`~maestro.escalation_bridge.prune_resolved_standdowns` leaves no
        orphan row. Each record stores as a single ``json.dumps`` blob keyed by
        ``number``, sorted for a deterministic on-disk order.
        """
        rows = [
            (number, json.dumps(record))
            for number, record in sorted(standdowns.items())
        ]
        self._conn.execute("DELETE FROM escalation_standdowns")
        self._conn.executemany(
            "INSERT INTO escalation_standdowns (number, data) VALUES (?, ?)",
            rows,
        )

    def mutate_escalation_standdowns(
        self, fn: Callable[[dict[int, StanddownRecord]], T],
    ) -> T:
        """Apply *fn* to the standdown map atomically; persist iff *fn* changed it.

        The whole load → *fn* → keyed-replace runs inside one ``BEGIN
        IMMEDIATE`` transaction, so the read-modify-write is serialised against
        every other ``state.db`` connection (a concurrent writer blocks on
        ``busy_timeout`` until this commit) — matching the
        :func:`~maestro.fileutil.file_lock` span the JSON store's
        :meth:`~maestro.escalation_bridge.JsonEscalationLedger.mutate` holds
        across its RMW, so an interleaved writer can never make this connection
        persist a stale map. The deep-copy change guard (the records are plain
        dicts/lists, so ``copy.deepcopy`` + ``==`` detects any change) preserves
        the "quiet no-op writes nothing" contract: an unchanged map runs no
        rewrite, and the empty transaction commits without touching the table.
        *fn* raising rolls the transaction back and re-raises. Mirrors
        :meth:`mutate_pr_ledger`.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            standdowns = self.load_escalation_standdowns()
            before = copy.deepcopy(standdowns)
            result = fn(standdowns)
            if standdowns != before:
                self._write_escalation_standdown_rows(standdowns)
            return result

    def seed_escalation_standdowns_once(
        self, standdowns: dict[int, StanddownRecord],
    ) -> None:
        """Seed the escalation-standdown table from *standdowns* exactly once.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_pr_ledger_once`: two connections racing their first seed
        over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both write. Acquiring the
        write lock up front serialises them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store carrying live standdowns is never overwritten by the frozen
        legacy JSON on a re-seed. *standdowns* comes from
        :func:`~maestro.escalation_bridge.load_all_escalation_standdowns`, so
        legacy ``"<pr>@<head>"`` entries arrive already collapsed to canonical
        ``number`` keys.
        """
        rows = [
            (number, json.dumps(record))
            for number, record in sorted(standdowns.items())
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_ESCALATION_STANDDOWNS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM escalation_standdowns",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO escalation_standdowns "
                    "(number, data) VALUES (?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_ESCALATION_STANDDOWNS_SEEDED_KEY,),
            )

    # -- question-resolution ledger ---------------------------------------

    def is_question_resolutions_seeded(self) -> bool:
        """Return whether the one-time question-resolution seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_QUESTION_RESOLUTIONS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_question_resolution(self, record: QuestionResolution) -> None:
        """Append one resolution row, auto-assigning its FIFO ``id``.

        The whole record stores as a single ``json.dumps(asdict(record))`` blob
        in ``data``, matching :data:`_QUESTION_RESOLUTIONS_DDL` — so additive
        ``QuestionResolution`` fields never force a table migration.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO question_resolutions (data) VALUES (?)",
                (json.dumps(asdict(record)),),
            )

    def load_question_resolutions(self) -> list[QuestionResolution]:
        """Return every resolution row oldest-first (``ORDER BY id ASC``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending
        read reproduces the JSONL forward (oldest-first) order. Each row's
        ``data`` blob is decoded and hydrated through
        :func:`~maestro.memory._parse_question_resolution_dict` — the same core
        the JSONL reader uses — so a blob written by a newer schema (extra keys)
        round-trips through the known fields, and any structurally-corrupt blob
        is dropped (not raised) exactly as the JSONL path drops it. A blob that
        is not even valid JSON is dropped by the same contract: the JSONL walk
        warns-and-skips a ``json.JSONDecodeError`` line, so this reader must not
        instead raise it out of a read its callers treat as storage-level (the
        audit-stall lineage gate softens only ``OSError``/``sqlite3.Error``).
        """
        cur = self._conn.execute(
            "SELECT data FROM question_resolutions ORDER BY id ASC",
        )
        records: list[QuestionResolution] = []
        for row in cur.fetchall():
            try:
                data = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Skipping corrupted question_resolution blob in state.db",
                )
                continue
            record = _parse_question_resolution_dict(data)
            if record is not None:
                records.append(record)
        return records

    def seed_question_resolutions_once(
        self, records: list[QuestionResolution],
    ) -> None:
        """Seed the question-resolution table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_rejection_history_once`: the ledger only appends (no
        idempotent replace and no unique key), so two connections racing their
        first seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`)
        would each pass a lock-free marker / emptiness check and both ``INSERT``,
        doubling the persisted history. Acquiring the write lock up front with
        ``BEGIN IMMEDIATE`` serialises them: the loser blocks on ``busy_timeout``,
        then re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a store populated by a prior
        run that crashed before stamping the marker is never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.memory.load_all_question_resolutions`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id ASC`` read matches the JSONL forward order.
        """
        rows = [(json.dumps(asdict(r)),) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_QUESTION_RESOLUTIONS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM question_resolutions",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO question_resolutions (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_QUESTION_RESOLUTIONS_SEEDED_KEY,),
            )

    # -- gate-activation trace ledger -------------------------------------

    def is_gate_activations_seeded(self) -> bool:
        """Return whether the one-time gate-activation seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_GATE_ACTIVATIONS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_gate_activation(self, record: GateActivation) -> None:
        """Append one gate-firing row, auto-assigning its FIFO ``id``.

        The whole record stores as a single ``json.dumps(asdict(record))`` blob
        in ``data``, matching :data:`_GATE_ACTIVATIONS_DDL` — so additive
        ``GateActivation`` fields never force a table migration.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO gate_activations (data) VALUES (?)",
                (json.dumps(asdict(record)),),
            )

    def load_gate_activations(self) -> list[GateActivation]:
        """Return every gate-firing row oldest-first (``ORDER BY id ASC``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending
        read reproduces the JSONL forward (oldest-first) order. Each row's
        ``data`` blob is decoded and hydrated through
        :func:`~maestro.audit_checks._parse_gate_activation_dict` — the same core
        the JSONL reader uses — so a blob written by a newer schema (extra keys)
        round-trips through the known fields, and any structurally-corrupt blob
        is dropped (not raised) exactly as the JSONL path drops it.
        """
        cur = self._conn.execute(
            "SELECT data FROM gate_activations ORDER BY id ASC",
        )
        records: list[GateActivation] = []
        for row in cur.fetchall():
            record = _parse_gate_activation_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        return records

    def load_gate_activations_tail(self, limit: int) -> list[GateActivation]:
        """Return the newest *limit* gate-firing rows oldest-first (bounded read).

        ``ORDER BY id DESC LIMIT ?`` reads at most *limit* rows from the tail,
        then reverses into the oldest-first order :meth:`load_gate_activations`
        returns, so a caller needing only a short tail pays O(limit), not
        O(history). ``limit <= 0`` returns ``[]``. Each blob hydrates through the
        same :func:`~maestro.audit_checks._parse_gate_activation_dict` core, so a
        structurally-corrupt blob is dropped (not raised), exactly as the full
        :meth:`load_gate_activations` read drops it.
        """
        if limit <= 0:
            return []
        cur = self._conn.execute(
            "SELECT data FROM gate_activations ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        records: list[GateActivation] = []
        for row in cur.fetchall():
            record = _parse_gate_activation_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        records.reverse()
        return records

    def load_gate_activations_tail_for_gate(
        self, gate: str, limit: int,
    ) -> list[GateActivation]:
        """Return the newest *limit* rows of *gate* oldest-first (bounded hydration).

        The gate-scoped sibling of :meth:`load_gate_activations_tail`: filters on
        the blob's ``$.gate`` so other gates' rows in the shared trace do not
        crowd the window (#4071). No ``gate`` column or index exists, so the
        ``json_extract`` filter is a scan; firings are low-cadence. ``limit <= 0`` returns ``[]``; a structurally-corrupt blob is
        dropped, not raised.
        """
        if limit <= 0:
            return []
        cur = self._conn.execute(
            "SELECT data FROM gate_activations"
            " WHERE json_extract(data, '$.gate') = ? ORDER BY id DESC LIMIT ?",
            (gate, limit),
        )
        records: list[GateActivation] = []
        for row in cur.fetchall():
            record = _parse_gate_activation_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        records.reverse()
        return records

    def seed_gate_activations_once(
        self, records: list[GateActivation],
    ) -> None:
        """Seed the gate-activation table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_question_resolutions_once`: the ledger only appends (no
        idempotent replace and no unique key), so two connections racing their
        first seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`)
        would each pass a lock-free marker / emptiness check and both ``INSERT``,
        doubling the persisted trace. Acquiring the write lock up front with
        ``BEGIN IMMEDIATE`` serialises them: the loser blocks on ``busy_timeout``,
        then re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a store populated by a prior
        run that crashed before stamping the marker is never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.audit_checks.load_all_gate_activations`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id ASC`` read matches the JSONL forward order.
        """
        rows = [(json.dumps(asdict(r)),) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_GATE_ACTIVATIONS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM gate_activations",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO gate_activations (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_GATE_ACTIVATIONS_SEEDED_KEY,),
            )

    # -- premise-observation ledger ---------------------------------------

    def is_premise_observations_seeded(self) -> bool:
        """Return whether the one-time premise-observation seed marker is set."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_PREMISE_OBSERVATIONS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_premise_observation(self, record: PremiseObservation) -> None:
        """Append one premise-measurement row, auto-assigning its FIFO ``id``.

        The whole record stores as a single ``json.dumps(asdict(record))`` blob
        in ``data``, matching :data:`_PREMISE_OBSERVATIONS_DDL` — so additive
        :class:`~maestro.audit_checks_calibration.PremiseObservation` fields never force a
        table migration. Mirrors :meth:`append_gate_activation`.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO premise_observations (data) VALUES (?)",
                (json.dumps(asdict(record)),),
            )

    def load_premise_observations(self) -> list[PremiseObservation]:
        """Return every premise-observation row oldest-first (``ORDER BY id``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending
        read reproduces the JSONL forward (oldest-first) order. Each row's
        ``data`` blob is decoded and hydrated through
        :func:`~maestro.audit_checks_calibration._parse_premise_observation_dict` — the same
        core the JSONL reader uses — so a blob written by a newer schema (extra
        keys) round-trips through the known fields, and any structurally-corrupt
        blob is dropped (not raised) exactly as the JSONL path drops it.
        """
        cur = self._conn.execute(
            "SELECT data FROM premise_observations ORDER BY id ASC",
        )
        records: list[PremiseObservation] = []
        for row in cur.fetchall():
            record = _parse_premise_observation_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        return records

    def seed_premise_observations_once(
        self, records: list[PremiseObservation],
    ) -> None:
        """Seed the premise table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_gate_activations_once`: the ledger only appends (no
        idempotent replace and no unique key), so two connections racing their
        first seed over the shared ``state.db`` would each pass a lock-free
        marker / emptiness check and both ``INSERT``, doubling the trace.
        Acquiring the write lock up front with ``BEGIN IMMEDIATE`` serialises
        them: the loser blocks, then re-reads the now-stamped marker inside its
        own transaction and skips. The insert stays guarded on an empty table so
        a store populated by a prior run that crashed before stamping the marker
        is never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.audit_checks_calibration.load_all_premise_observations`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id ASC`` read matches the JSONL forward order.
        """
        rows = [(json.dumps(asdict(r)),) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_PREMISE_OBSERVATIONS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM premise_observations",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO premise_observations (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_PREMISE_OBSERVATIONS_SEEDED_KEY,),
            )

    # -- prediction sensor ledger -----------------------------------------

    def is_predictions_seeded(self) -> bool:
        """Return whether the one-time prediction seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_PREDICTIONS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_prediction(self, record: dict[str, object]) -> None:
        """Append one prediction row, auto-assigning its FIFO ``id``.

        The whole record stores as a single ``json.dumps(record)`` blob in
        ``data``, matching :data:`_PREDICTIONS_DDL` — so the three heterogeneous
        row shapes round-trip verbatim and additive fields never force a table
        migration.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO predictions (data) VALUES (?)",
                (json.dumps(record),),
            )

    def load_predictions(self) -> list[dict[str, object]]:
        """Return every prediction row oldest-first (``ORDER BY id ASC``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending
        read reproduces the JSONL forward (oldest-first) order the readers take a
        tail of. Each row's ``data`` blob is decoded; a blob that does not decode
        to a JSON object is dropped (not raised), mirroring
        :func:`~maestro.audit_checks_calibration._read_prediction_rows`'s dict-only filter.
        """
        cur = self._conn.execute(
            "SELECT data FROM predictions ORDER BY id ASC",
        )
        records: list[dict[str, object]] = []
        for row in cur.fetchall():
            record = json.loads(row["data"])
            if isinstance(record, dict):
                records.append(record)
        return records

    def seed_predictions_once(
        self, records: list[dict[str, object]],
    ) -> None:
        """Seed the prediction table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_gate_activations_once`: the ledger only appends (no
        idempotent replace and no unique key), so two connections racing their
        first seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`)
        would each pass a lock-free marker / emptiness check and both ``INSERT``,
        doubling the persisted trace. Acquiring the write lock up front with
        ``BEGIN IMMEDIATE`` serialises them: the loser blocks on ``busy_timeout``,
        then re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a store populated by a prior
        run that crashed before stamping the marker is never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.audit_checks_calibration.load_all_predictions`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id ASC`` read matches the JSONL forward order.
        """
        rows = [(json.dumps(r),) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_PREDICTIONS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM predictions",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO predictions (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_PREDICTIONS_SEEDED_KEY,),
            )

    # -- owned-backlog ledger ---------------------------------------------

    def is_owned_backlog_seeded(self) -> bool:
        """Return whether the one-time owned-backlog seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_OWNED_BACKLOG_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_backlog_snapshot(self, snapshot: BacklogSnapshot) -> None:
        """Append one snapshot row, auto-assigning its FIFO ``id``."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO backlog_snapshots "
                "(iteration, count, timestamp) VALUES (?, ?, ?)",
                (snapshot.iteration, snapshot.count, snapshot.timestamp),
            )

    def seed_backlog_snapshots_once(
        self, snapshots: list[BacklogSnapshot],
    ) -> None:
        """Seed the snapshot table from *snapshots* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_deeds_once`: the ledger only appends (no idempotent
        replace), so two connections racing their first seed over the shared
        ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each pass a lock-free
        marker / emptiness check and both ``INSERT``, doubling the
        cycle-over-cycle history. Acquiring the write lock up front with
        ``BEGIN IMMEDIATE`` serializes them: the loser blocks on ``busy_timeout``,
        then re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a store populated by a
        prior run that crashed before stamping the marker is never doubled
        either.

        *snapshots* is oldest-first (see
        :func:`~maestro.stats.load_all_backlog_snapshots`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id DESC`` read is newest-first, matching the JSONL reverse
        read.
        """
        rows = [(s.iteration, s.count, s.timestamp) for s in snapshots]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_OWNED_BACKLOG_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM backlog_snapshots",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO backlog_snapshots "
                    "(iteration, count, timestamp) VALUES (?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_OWNED_BACKLOG_SEEDED_KEY,),
            )

    def recent_backlog_snapshots(self, limit: int) -> list[BacklogSnapshot]:
        """Return the most recent *limit* snapshots, newest-first.

        Selects the highest-``id`` rows (``ORDER BY id DESC``), matching the JSONL
        reverse read. A non-positive *limit* returns an empty list — SQLite
        treats a negative ``LIMIT`` as unbounded, so the guard is load-bearing.
        """
        if limit <= 0:
            return []
        cur = self._conn.execute(
            "SELECT iteration, count, timestamp FROM backlog_snapshots "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            BacklogSnapshot(
                iteration=row["iteration"],
                count=row["count"],
                timestamp=row["timestamp"],
            )
            for row in cur.fetchall()
        ]

    # -- pr-provenance ledger ---------------------------------------------

    def is_pr_provenance_seeded(self) -> bool:
        """Return whether the one-time PR-provenance seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_PR_PROVENANCE_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_provenance(self, record: ProvenanceRecord) -> None:
        """Append one admission row, auto-assigning its FIFO ``id``."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO provenance (pr_number, channel) VALUES (?, ?)",
                (record.pr_number, record.channel),
            )

    def seed_provenance_once(self, records: list[ProvenanceRecord]) -> None:
        """Seed the provenance table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_deeds_once`: the ledger only appends (no idempotent
        replace), so two connections racing their first seed over the shared
        ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each pass a lock-free
        marker / emptiness check and both ``INSERT``, doubling the readmission
        window. Acquiring the write lock up front with ``BEGIN IMMEDIATE``
        serializes them: the loser blocks on ``busy_timeout``, then re-reads the
        now-stamped marker inside its own transaction and skips. The insert stays
        guarded on an empty table so a store populated by a prior run that
        crashed before stamping the marker is never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.remediation_tracker.load_all_provenance_records`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id DESC`` read is newest-first, matching the JSONL reverse
        read.
        """
        rows = [(r.pr_number, r.channel) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_PR_PROVENANCE_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM provenance",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO provenance (pr_number, channel) "
                    "VALUES (?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_PR_PROVENANCE_SEEDED_KEY,),
            )

    def recent_provenance(self, limit: int) -> list[ProvenanceRecord]:
        """Return the most recent *limit* admissions, newest-first.

        Selects the highest-``id`` rows (``ORDER BY id DESC``), matching the JSONL
        trailing-window walk. A non-positive *limit* returns an empty list —
        SQLite treats a negative ``LIMIT`` as unbounded, so the guard is
        load-bearing.
        """
        if limit <= 0:
            return []
        cur = self._conn.execute(
            "SELECT pr_number, channel FROM provenance "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            ProvenanceRecord(
                pr_number=row["pr_number"],
                channel=row["channel"],
            )
            for row in cur.fetchall()
        ]

    # -- settlement-reconciliation ledger ---------------------------------

    def is_settlement_reconciliations_seeded(self) -> bool:
        """Return whether the one-time settlement-reconciliation seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_SETTLEMENT_RECONCILIATIONS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_settlement_reconciliation(
        self, record: SettlementReconciliationRecord,
    ) -> None:
        """Append one reconciler-driven store-change row, auto-assigning its FIFO ``id``.

        ``cleared`` is stored as ``INTEGER`` 0/1 and ``detail`` may be ``NULL``,
        matching :data:`_SETTLEMENT_RECONCILIATIONS_DDL`.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO settlement_reconciliations "
                "(pr_number, reconciler, cleared, detail, at_iteration) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.pr_number,
                    record.reconciler,
                    int(record.cleared),
                    record.detail,
                    record.at_iteration,
                ),
            )

    def load_settlement_reconciliations(
        self,
    ) -> list[SettlementReconciliationRecord]:
        """Return every reconciliation row oldest-first (``ORDER BY id ASC``).

        The seed / test read-back: the ``AUTOINCREMENT`` ``id`` preserves append
        order, so an ascending read reproduces the JSONL append (oldest-first)
        order. ``cleared`` is decoded back from its ``INTEGER`` 0/1 storage and a
        ``NULL`` ``detail`` round-trips to ``None``.
        """
        cur = self._conn.execute(
            "SELECT pr_number, reconciler, cleared, detail, at_iteration "
            "FROM settlement_reconciliations ORDER BY id ASC",
        )
        return [
            SettlementReconciliationRecord(
                pr_number=row["pr_number"],
                reconciler=row["reconciler"],
                cleared=bool(row["cleared"]),
                detail=row["detail"],
                at_iteration=row["at_iteration"],
            )
            for row in cur.fetchall()
        ]

    def seed_settlement_reconciliations_once(
        self, records: list[SettlementReconciliationRecord],
    ) -> None:
        """Seed the settlement-reconciliation table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_provenance_once`: the ledger only appends (no idempotent
        replace), so two connections racing their first seed over the shared
        ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each pass a lock-free
        marker / emptiness check and both ``INSERT``, doubling the trace.
        Acquiring the write lock up front with ``BEGIN IMMEDIATE`` serializes
        them: the loser blocks on ``busy_timeout``, then re-reads the now-stamped
        marker inside its own transaction and skips. The insert stays guarded on
        an empty table so a store populated by a prior run that crashed before
        stamping the marker is never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.settlement_record_reconciler.load_all_settlement_reconciliations`),
        so the ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id ASC`` read matches the JSONL append order.
        """
        rows = [
            (
                r.pr_number,
                r.reconciler,
                int(r.cleared),
                r.detail,
                r.at_iteration,
            )
            for r in records
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_SETTLEMENT_RECONCILIATIONS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM settlement_reconciliations",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO settlement_reconciliations "
                    "(pr_number, reconciler, cleared, detail, at_iteration) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_SETTLEMENT_RECONCILIATIONS_SEEDED_KEY,),
            )

    # -- obligation-fork ledger -------------------------------------------

    def is_obligation_fork_seeded(self) -> bool:
        """Return whether the one-time obligation-fork seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_OBLIGATION_FORK_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_obligation_fork(self, record: ObligationForkRecord) -> None:
        """Append one marker, honouring the ``(task_id, pr_number)`` unique key.

        ``INSERT OR IGNORE`` reproduces the JSONL writer's idempotency: a marker
        already stored for this ``(task_id, pr_number)`` pair is a no-op, so a
        second call in the same turn does not duplicate the row (first-wins).
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO obligation_forks "
                "(task_id, pr_number, head_sha, remote_sha, branch, "
                "remote_is_ancestor) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.task_id,
                    record.pr_number,
                    record.head_sha,
                    record.remote_sha,
                    record.branch,
                    int(record.remote_is_ancestor),
                ),
            )

    def seed_obligation_forks_once(
        self, records: list[ObligationForkRecord],
    ) -> None:
        """Seed the obligation-fork table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_provenance_once`: two connections racing their first seed
        over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both ``INSERT``. Acquiring
        the write lock up front serializes them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either, and ``INSERT OR IGNORE`` honours the
        ``(task_id, pr_number)`` unique key so a legacy file with a duplicate
        pair never violates the constraint.

        *records* is oldest-first (see
        :func:`~maestro.remediation_tracker.load_all_obligation_forks`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id DESC`` read is last-match, matching the JSONL reader.
        """
        rows = [
            (
                r.task_id,
                r.pr_number,
                r.head_sha,
                r.remote_sha,
                r.branch,
                int(r.remote_is_ancestor),
            )
            for r in records
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_OBLIGATION_FORK_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM obligation_forks",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO obligation_forks "
                    "(task_id, pr_number, head_sha, remote_sha, branch, "
                    "remote_is_ancestor) VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_OBLIGATION_FORK_SEEDED_KEY,),
            )

    def get_obligation_fork_for_task(
        self, task_id: str,
    ) -> ObligationFork | None:
        """Return the last marker for *task_id* as an :class:`ObligationFork`.

        Selects the highest-``id`` row matching *task_id* (``ORDER BY id DESC
        LIMIT 1``) — the SQLite analogue of the JSONL reader's last-matching-row
        walk (:func:`~maestro.remediation_tracker.get_obligation_fork`). Returns
        ``None`` when *task_id* is empty or no row matches. The stored
        ``remote_is_ancestor`` 0/1 integer is coerced back to ``bool``.
        """
        if not task_id:
            return None
        row = self._conn.execute(
            "SELECT head_sha, remote_sha, branch, remote_is_ancestor "
            "FROM obligation_forks WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return ObligationFork(
            head_sha=row["head_sha"],
            remote_sha=row["remote_sha"],
            branch=row["branch"],
            remote_is_ancestor=bool(row["remote_is_ancestor"]),
        )

    # -- fetch-truth ledger -----------------------------------------------

    def is_fetch_truth_seeded(self) -> bool:
        """Return whether the one-time fetch-truth seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_FETCH_TRUTH_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_fetch_truth(self, record: FetchTruthRecord) -> None:
        """Append one fetch-truth marker — one row per call, no dedupe.

        Mirrors the JSONL writer
        (:func:`~maestro.remediation_tracker.record_fetch_truth`), which appends
        unconditionally rather than deduping per ``(task_id, pr_number)`` the way
        the obligation-fork ledger does — so the ``fetch_truth_markers`` table
        carries no unique key and this is a plain ``INSERT``. ``is_ancestor``
        narrows to ``INTEGER`` 0/1.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO fetch_truth_markers "
                "(task_id, pr_number, is_ancestor, head_sha) "
                "VALUES (?, ?, ?, ?)",
                (
                    record.task_id,
                    record.pr_number,
                    int(record.is_ancestor),
                    record.head_sha,
                ),
            )

    def seed_fetch_truth_once(
        self, records: list[FetchTruthRecord],
    ) -> None:
        """Seed the fetch-truth table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_obligation_forks_once`: two connections racing their first
        seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would
        each pass a lock-free marker / emptiness check and both ``INSERT``.
        Acquiring the write lock up front serializes them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either. Unlike the obligation-fork seed there is no unique
        key — the JSONL writer appends one row per call — so a plain ``INSERT``
        is used and *records* preserves every marker.

        *records* is oldest-first (see
        :func:`~maestro.remediation_tracker.load_all_fetch_truth_markers`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order.
        """
        rows = [
            (r.task_id, r.pr_number, int(r.is_ancestor), r.head_sha)
            for r in records
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_FETCH_TRUTH_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM fetch_truth_markers",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO fetch_truth_markers "
                    "(task_id, pr_number, is_ancestor, head_sha) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_FETCH_TRUTH_SEEDED_KEY,),
            )

    def has_fetch_truth_for_task(self, task_id: str) -> bool:
        """Return whether any fetch-truth marker exists for *task_id*.

        The SQLite analogue of
        :func:`~maestro.remediation_tracker.has_fetch_truth`: an ``EXISTS``
        lookup on ``task_id``. Returns ``False`` for an empty *task_id*, which
        the JSONL reader also rejects — a marker carries no empty key.
        """
        if not task_id:
            return False
        row = self._conn.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM fetch_truth_markers WHERE task_id = ?"
            ") AS present",
            (task_id,),
        ).fetchone()
        return bool(row["present"])

    def fetch_truth_relap_exists(
        self, pr_number: int, head_sha: str,
    ) -> bool:
        """Return whether *(pr_number, head_sha)* was proved unlanded before.

        The SQLite analogue of
        :func:`~maestro.remediation_tracker.fetch_truth_relap`: an ``EXISTS``
        lookup for a marker whose ``pr_number`` and ``head_sha`` match and whose
        ``is_ancestor`` is 0 (git said not-yet-on-``main``). Returns ``False`` for
        an empty *head_sha* (an unresolvable head must never divert, matching the
        JSONL reader), so a legacy row stored with the ``''`` default head is
        never matched.
        """
        if not head_sha:
            return False
        row = self._conn.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM fetch_truth_markers "
            "WHERE pr_number = ? AND head_sha = ? AND is_ancestor = 0"
            ") AS present",
            (pr_number, head_sha),
        ).fetchone()
        return bool(row["present"])

    # -- preflight-pass ledger --------------------------------------------

    def is_preflight_passes_seeded(self) -> bool:
        """Return whether the one-time preflight-pass seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_PREFLIGHT_PASSES_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def record_preflight_pass(self, pr_number: int, head_sha: str) -> None:
        """Upsert *pr_number*'s latest passing head SHA — a keyed replace.

        The SQLite analogue of
        :func:`~maestro.remediation_tracker.record_preflight_pass`: an
        ``INSERT OR REPLACE`` on the ``pr_number`` primary key reproduces the
        JSON dict's "only the most recent confirmed-passing head matters",
        overwriting any prior recorded SHA for the PR.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO preflight_passes (pr_number, head_sha) "
                "VALUES (?, ?)",
                (pr_number, head_sha),
            )

    def should_skip_preflight(self, pr_number: int, head_sha: str) -> bool:
        """Return whether *pr_number*'s *head_sha* already passed preflight.

        The SQLite analogue of
        :func:`~maestro.remediation_tracker.should_skip_routing`: an exact
        ``(pr_number, head_sha)`` ``EXISTS`` match (one row per PR, so this is
        the recorded head equalling *head_sha*). Returns ``False`` for an empty
        *head_sha* — an unresolvable head must never suppress remediation,
        matching the JSON reader.
        """
        if not head_sha:
            return False
        row = self._conn.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM preflight_passes "
            "WHERE pr_number = ? AND head_sha = ?"
            ") AS present",
            (pr_number, head_sha),
        ).fetchone()
        return bool(row["present"])

    def seed_preflight_passes_once(
        self, records: list[PreflightPassRecord],
    ) -> None:
        """Seed the preflight-pass table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_fetch_truth_once`: two connections racing their first seed
        over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both write. Acquiring the
        write lock up front serializes them: the loser blocks on ``busy_timeout``,
        then re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a live store carrying newer
        passing heads is never overwritten by stale legacy rows on a re-seed —
        even though the per-key ``INSERT OR REPLACE`` is itself idempotent.
        """
        rows = [(r.pr_number, r.head_sha) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_PREFLIGHT_PASSES_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM preflight_passes",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO preflight_passes "
                    "(pr_number, head_sha) VALUES (?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_PREFLIGHT_PASSES_SEEDED_KEY,),
            )

    # -- dropped-directive ledger -----------------------------------------

    def is_dropped_directives_seeded(self) -> bool:
        """Return whether the one-time dropped-directive seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_DROPPED_DIRECTIVES_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def offer_dropped_directive(self, entry: DroppedDirective) -> None:
        """Append *entry*, then bound the table to ``_MAX_ENTRIES`` — one txn.

        The SQLite analogue of
        :meth:`~maestro.directive_ledger.JsonlDroppedDirectiveLedger.offer`: an
        ``INSERT`` (the ``AUTOINCREMENT`` ``id`` preserving FIFO order) followed
        by a trim that deletes every row outside the newest ``_MAX_ENTRIES`` by
        ``id``, dropping the oldest surplus exactly as the JSONL body slices
        ``entries[-_MAX_ENTRIES:]``. Both statements run inside one transaction so
        a reader never observes the transient over-cap state; ``BEGIN IMMEDIATE``
        takes the write lock before the dedupe read. Idempotent: a row
        already matching *entry* on ``text``, ``pr_numbers`` and ``shas`` (compared
        on the canonical JSON encoding) short-circuits both statements, keeping
        the original row and its ``recorded_at``.
        """
        row = _dropped_directive_to_row(entry)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._conn.execute(
                "SELECT 1 FROM dropped_directives "
                "WHERE text = ? AND pr_numbers = ? AND shas = ? LIMIT 1",
                (row[0], row[2], row[3]),
            ).fetchone() is not None:
                return
            self._conn.execute(
                "INSERT INTO dropped_directives "
                "(text, source, pr_numbers, shas, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                row,
            )
            self._conn.execute(
                "DELETE FROM dropped_directives WHERE id NOT IN ("
                "SELECT id FROM dropped_directives ORDER BY id DESC LIMIT ?"
                ")",
                (_MAX_ENTRIES,),
            )

    def replay_dropped_directive(
        self, is_dead: Callable[[DroppedDirective], bool],
    ) -> str | None:
        """Prune dead rows, consume + return the oldest live entry — one txn.

        The SQLite analogue of
        :meth:`~maestro.directive_ledger.JsonlDroppedDirectiveLedger.replay`:
        load every row oldest-first, partition via *is_dead* (evaluated in Python
        so the module keeps the git / age-out policy), delete the dead rows
        **and** the oldest live one, and return that entry's ``text`` — or
        ``None`` when nothing lives (the dead rows are still pruned). The load and
        the deletes run inside one ``BEGIN IMMEDIATE`` transaction — the write
        lock is taken before the read, so a concurrent consumer blocks on
        ``busy_timeout`` until this commit rather than replaying the same
        banked directive twice.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "SELECT id, text, source, pr_numbers, shas, recorded_at "
                "FROM dropped_directives ORDER BY id ASC",
            )
            rows = cur.fetchall()
            if not rows:
                return None
            live: list[sqlite3.Row] = []
            dead_ids: list[int] = []
            for row in rows:
                if is_dead(_row_to_dropped_directive(row)):
                    dead_ids.append(row["id"])
                else:
                    live.append(row)
            if not live:
                self._conn.execute("DELETE FROM dropped_directives")
                return None
            offered = live[0]
            offered_text: str = offered["text"]
            remove_ids = [*dead_ids, offered["id"]]
            self._conn.executemany(
                "DELETE FROM dropped_directives WHERE id = ?",
                [(rid,) for rid in remove_ids],
            )
            return offered_text

    def seed_dropped_directives_once(
        self, records: list[DroppedDirective],
    ) -> None:
        """Seed the dropped-directive table from *records* once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_fetch_truth_once`: two connections racing their first seed
        over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both ``INSERT``. Acquiring
        the write lock up front serializes them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.directive_ledger.load_all_dropped_directives`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the JSONL append order — a later
        ``ORDER BY id ASC`` replay walk then matches the JSONL oldest-first read.
        """
        rows = [_dropped_directive_to_row(r) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_DROPPED_DIRECTIVES_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM dropped_directives",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO dropped_directives "
                    "(text, source, pr_numbers, shas, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_DROPPED_DIRECTIVES_SEEDED_KEY,),
            )

    # -- settled-verdicts pre-filter --------------------------------------

    def is_settled_verdicts_seeded(self) -> bool:
        """Return whether the one-time settled-verdicts seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_SETTLED_VERDICTS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def effective_settled_prs(self) -> frozenset[int]:
        """Return the PRs whose collapsed row is a settle — the effective set.

        The SQLite analogue of
        :meth:`~maestro.audit_checks_settlement.JsonlSettledVerdictsLedger.effective_settled_prs`:
        because ``pr_number`` is the PRIMARY KEY and every write is an
        ``INSERT OR REPLACE``, the stored row per PR *is* its last-row-wins state,
        so the settle set is a plain ``WHERE settled = 1`` scan.
        """
        cur = self._conn.execute(
            "SELECT pr_number FROM settled_verdicts WHERE settled = 1",
        )
        return frozenset(row["pr_number"] for row in cur.fetchall())

    def merged_settled_prs(self) -> frozenset[int]:
        """Return the PRs whose collapsed row is a merged settle.

        The SQLite analogue of
        :meth:`~maestro.audit_checks_settlement.JsonlSettledVerdictsLedger.merged_settled_prs`:
        a ``WHERE settled = 1 AND kind = 'merged'`` scan, so a closed settle
        (``kind = 'closed'``) and a retraction (``settled = 0``) are both
        excluded, matching the JSONL walk's ``kind == "merged"`` collapse.
        """
        cur = self._conn.execute(
            "SELECT pr_number FROM settled_verdicts "
            "WHERE settled = 1 AND kind = 'merged'",
        )
        return frozenset(row["pr_number"] for row in cur.fetchall())

    def append_settled_verdicts(
        self, records: list[SettledVerdictRecord],
    ) -> None:
        """Upsert *records* (settle and/or retraction) — one keyed-replace batch.

        Each record is an ``INSERT OR REPLACE`` on the ``pr_number`` primary key,
        so processing the batch in order reproduces the JSONL append's
        last-row-wins collapse: a later record for a PR supersedes an earlier one.
        Nothing runs for an empty batch.
        """
        rows = [_settled_verdict_to_row(r) for r in records]
        if not rows:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO settled_verdicts "
                "(pr_number, settled, kind, settled_at_iteration, "
                "retracted_at_iteration, verdict_task_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def seed_settled_verdicts_once(
        self, records: list[SettledVerdictRecord],
    ) -> None:
        """Seed the settled-verdicts table from *records* once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_preflight_passes_once`: two connections racing their first
        seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both write. Acquiring the
        write lock up front serializes them: the loser blocks on ``busy_timeout``,
        then re-reads the now-stamped marker inside its own transaction and skips.
        The insert stays guarded on an empty table so a live store carrying newer
        settle/retract state is never overwritten by stale legacy rows on a
        re-seed — even though the per-key ``INSERT OR REPLACE`` is itself
        idempotent. *records* is oldest-first (see
        :func:`~maestro.audit_checks_settlement.load_all_settled_verdicts`), so the
        keyed-replace batch reproduces the JSONL last-row-wins collapse.
        """
        rows = [_settled_verdict_to_row(r) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_SETTLED_VERDICTS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM settled_verdicts",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO settled_verdicts "
                    "(pr_number, settled, kind, settled_at_iteration, "
                    "retracted_at_iteration, verdict_task_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_SETTLED_VERDICTS_SEEDED_KEY,),
            )

    # -- stand-down verdict ledger ----------------------------------------

    def is_stand_down_verdicts_seeded(self) -> bool:
        """Return whether the one-time stand-down-verdict seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_STAND_DOWN_VERDICTS_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_stand_down_verdict(self, row: StandDownRow) -> None:
        """Append one verdict row — a single INSERT (append-only, no unique key)."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO stand_down_verdicts "
                "(pr, head_sha, recorded_at, released_at, branch_ahead_of_origin) "
                "VALUES (?, ?, ?, ?, ?)",
                _stand_down_row_to_insert(row),
            )

    def append_stand_down_release(self, pr: int, released_at: str) -> None:
        """Append one head-agnostic release row — ``head_sha``/``recorded_at`` NULL."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO stand_down_verdicts "
                "(pr, head_sha, recorded_at, released_at, branch_ahead_of_origin) "
                "VALUES (?, NULL, NULL, ?, NULL)",
                (pr, released_at),
            )

    def stand_down_find(self, pr: int, head_sha: str) -> StandDownVerdict | None:
        """Return the release-aware last-write-wins ``(pr, head_sha)`` verdict.

        Loads *pr*'s rows in ``id`` (append) order and runs the same scan the
        JSONL adapter does (mirroring :meth:`replay_dropped_directive`'s
        policy-in-Python approach): a verdict row (non-NULL ``head_sha`` +
        ``recorded_at``) whose head matches sets the running match; a release row
        (non-NULL ``released_at``) voids any earlier match for the PR. The last
        surviving match wins.
        """
        cur = self._conn.execute(
            "SELECT head_sha, recorded_at, released_at, branch_ahead_of_origin "
            "FROM stand_down_verdicts WHERE pr = ? ORDER BY id ASC",
            (pr,),
        )
        found: StandDownVerdict | None = None
        for row in cur.fetchall():
            if row["head_sha"] is not None and row["recorded_at"] is not None:
                if row["head_sha"] == head_sha:
                    ahead = row["branch_ahead_of_origin"]
                    found = StandDownVerdict(
                        pr=pr,
                        head_sha=row["head_sha"],
                        recorded_at=row["recorded_at"],
                        branch_ahead_of_origin=(
                            None if ahead is None else bool(ahead)
                        ),
                    )
                continue
            if row["released_at"] is not None:
                found = None
        return found

    def stand_down_has_live(self, pr: int) -> bool:
        """Return whether *pr* carries an unreleased verdict (release-aware scan)."""
        cur = self._conn.execute(
            "SELECT head_sha, recorded_at, released_at "
            "FROM stand_down_verdicts WHERE pr = ? ORDER BY id ASC",
            (pr,),
        )
        live = False
        for row in cur.fetchall():
            if row["head_sha"] is not None and row["recorded_at"] is not None:
                live = True
                continue
            if row["released_at"] is not None:
                live = False
        return live

    def stand_down_verdict_rows(self) -> list[StandDownRow]:
        """Return every genuine verdict row oldest-first (release rows excluded)."""
        cur = self._conn.execute(
            "SELECT pr, head_sha, recorded_at, branch_ahead_of_origin "
            "FROM stand_down_verdicts "
            "WHERE pr > 0 AND head_sha IS NOT NULL AND recorded_at IS NOT NULL "
            "ORDER BY id ASC",
        )
        return [_row_to_stand_down_row(row) for row in cur.fetchall()]

    def stand_down_written_count(self) -> int:
        """Return the all-time count of verdict rows (release rows excluded)."""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM stand_down_verdicts "
            "WHERE pr IS NOT NULL AND head_sha IS NOT NULL "
            "AND recorded_at IS NOT NULL",
        )
        return int(cur.fetchone()["n"])

    def stand_down_recorded_timestamps(self) -> list[str]:
        """Return the non-empty ``recorded_at`` of every row carrying one."""
        cur = self._conn.execute(
            "SELECT recorded_at FROM stand_down_verdicts "
            "WHERE recorded_at IS NOT NULL AND recorded_at != '' ORDER BY id ASC",
        )
        return [row["recorded_at"] for row in cur.fetchall()]

    def stand_down_banked_pr_ids(self) -> set[int]:
        """Return the positive-int ``pr`` of every row (verdict *and* release)."""
        cur = self._conn.execute(
            "SELECT DISTINCT pr FROM stand_down_verdicts WHERE pr > 0",
        )
        return {row["pr"] for row in cur.fetchall()}

    def stand_down_verdict_count_for_pr(self, pr: int) -> int:
        """Return how many verdict rows (non-NULL ``recorded_at``) are keyed to *pr*."""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM stand_down_verdicts "
            "WHERE pr = ? AND recorded_at IS NOT NULL",
            (pr,),
        )
        return int(cur.fetchone()["n"])

    def seed_stand_down_verdicts_once(
        self, records: list[StandDownRow],
    ) -> None:
        """Seed the stand-down-verdict table from *records* once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_fetch_truth_once`: two connections racing their first seed over
        the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each pass a
        lock-free marker / emptiness check and both ``INSERT``. Acquiring the write
        lock up front serializes them: the loser blocks on ``busy_timeout``, then
        re-reads the now-stamped marker inside its own transaction and skips. The
        insert stays guarded on an empty table so a store populated by a prior run
        that crashed before stamping the marker is never doubled either. *records*
        is oldest-first (see
        :func:`~maestro.remediation_tracker.load_all_stand_down_records`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the JSONL append order the release-aware
        scan depends on. Append-only, no unique key — one row per record, mirroring
        :meth:`seed_fetch_truth_once`.
        """
        rows = [_stand_down_row_to_insert(r) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_STAND_DOWN_VERDICTS_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM stand_down_verdicts",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO stand_down_verdicts "
                    "(pr, head_sha, recorded_at, released_at, branch_ahead_of_origin) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_STAND_DOWN_VERDICTS_SEEDED_KEY,),
            )

    # -- unconfirmed-own-landing sidecar ----------------------------------

    def is_unconfirmed_landing_seeded(self) -> bool:
        """Return whether the one-time unconfirmed-landing seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_UNCONFIRMED_LANDING_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def load_unconfirmed_landing_seen(self) -> dict[str, int]:
        """Return the stored ``{sha: first_seen_iteration}`` map."""
        cur = self._conn.execute(
            "SELECT sha, first_seen_iteration FROM unconfirmed_landing_seen",
        )
        return {
            row["sha"]: row["first_seen_iteration"] for row in cur.fetchall()
        }

    def prune_and_touch_unconfirmed_landing(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        """Prune the first-seen map to *observed*, touching new SHAs — one txn.

        The SQLite analogue of the JSON adapter's ``file_lock``-guarded
        load-prune-write, and the direct parity of the other bounded-RMW ledger
        :meth:`replay_dropped_directive`: inside **one** ``BEGIN IMMEDIATE``
        transaction, load the stored ``{sha: first_seen}`` map, recompute
        ``{sha: current.get(sha, iteration) for sha in observed}`` — dropping every
        SHA no longer observed (it landed or settled) and stamping a first-seen of
        *iteration* on any newly-seen SHA — replace the whole table with the result
        (a ``DELETE`` of every row followed by an ``executemany`` INSERT of the
        survivors, mirroring :meth:`replace_task_queue`), and return the map for the
        rule's findings loop. Owning the read *and* the write in one method under
        one transaction — rather than composing a separate load then replace at the
        adapter, and taking the write lock up front rather than letting the read
        run in autocommit — keeps a concurrent writer from interleaving between
        them: it blocks on ``busy_timeout`` until this commit. An empty
        *observed* clears the table (the prune dropped every SHA), reproducing the
        JSON adapter's ``{}`` write.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self.load_unconfirmed_landing_seen()
            updated = {sha: current.get(sha, iteration) for sha in observed}
            self._conn.execute("DELETE FROM unconfirmed_landing_seen")
            self._conn.executemany(
                "INSERT INTO unconfirmed_landing_seen "
                "(sha, first_seen_iteration) VALUES (?, ?)",
                list(updated.items()),
            )
        return updated

    def seed_unconfirmed_landing_once(self, mapping: dict[str, int]) -> None:
        """Seed the first-seen table from *mapping* once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_dropped_directives_once`: two connections racing their first
        seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both ``INSERT``. Acquiring
        the write lock up front serializes them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either.
        """
        rows = list(mapping.items())
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_UNCONFIRMED_LANDING_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM unconfirmed_landing_seen",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO unconfirmed_landing_seen "
                    "(sha, first_seen_iteration) VALUES (?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_UNCONFIRMED_LANDING_SEEDED_KEY,),
            )

    # -- orphaned-cure sidecar --------------------------------------------

    def is_orphaned_cure_seeded(self) -> bool:
        """Return whether the one-time orphaned-cure seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_ORPHANED_CURE_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def load_orphaned_cure_seen(self) -> dict[str, int]:
        """Return the stored ``{plan_id: first_seen_iteration}`` map."""
        cur = self._conn.execute(
            "SELECT plan_id, first_seen_iteration FROM orphaned_cure_seen",
        )
        return {
            row["plan_id"]: row["first_seen_iteration"] for row in cur.fetchall()
        }

    def prune_and_touch_orphaned_cure(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        """Prune the first-seen map to *observed*, touching new plan ids — one txn.

        The SQLite analogue of the JSON adapter's ``file_lock``-guarded
        load-prune-write, and the direct parity of
        :meth:`prune_and_touch_unconfirmed_landing`: inside **one** ``BEGIN
        IMMEDIATE`` transaction, load the stored ``{plan_id: first_seen}`` map,
        recompute ``{pid: current.get(pid, iteration) for pid in observed}`` —
        dropping every plan id no longer observed (it left ``implemented``) and
        stamping a first-seen of *iteration* on any newly-seen plan id — replace
        the whole table with the result (a ``DELETE`` of every row followed by an
        ``executemany`` INSERT of the survivors, mirroring
        :meth:`replace_task_queue`), and return the map for the rule's findings
        loop. Owning the read *and* the write in one method under one
        transaction, with the write lock taken up front rather than letting the
        read run in autocommit, keeps a concurrent writer from interleaving
        between them: it blocks on ``busy_timeout`` until this commit. An empty
        *observed* clears the table, reproducing the JSON adapter's ``{}`` write.
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self.load_orphaned_cure_seen()
            updated = {pid: current.get(pid, iteration) for pid in observed}
            self._conn.execute("DELETE FROM orphaned_cure_seen")
            self._conn.executemany(
                "INSERT INTO orphaned_cure_seen "
                "(plan_id, first_seen_iteration) VALUES (?, ?)",
                list(updated.items()),
            )
        return updated

    def seed_orphaned_cure_once(self, mapping: dict[str, int]) -> None:
        """Seed the first-seen table from *mapping* once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_unconfirmed_landing_once`: two connections racing their first
        seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would
        each pass a lock-free marker / emptiness check and both ``INSERT``.
        Acquiring the write lock up front serializes them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either.
        """
        rows = list(mapping.items())
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_ORPHANED_CURE_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM orphaned_cure_seen",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO orphaned_cure_seen "
                    "(plan_id, first_seen_iteration) VALUES (?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_ORPHANED_CURE_SEEDED_KEY,),
            )

    # -- impact-review surfaced-set sidecar -------------------------------

    def is_impact_review_surfaced_seeded(self) -> bool:
        """Return whether the one-time impact-review seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_IMPACT_REVIEW_SURFACED_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def load_impact_review_surfaced(self) -> set[str]:
        """Return the stored surfaced-version-id set."""
        cur = self._conn.execute(
            "SELECT version_id FROM impact_review_surfaced",
        )
        return {row["version_id"] for row in cur.fetchall()}

    def record_impact_review_surfaced(self, version_ids: list[str]) -> None:
        """Idempotently union *version_ids* into the surfaced set — one txn.

        The SQLite analogue of the JSON adapter's ``file_lock``-guarded
        load-union-write: one ``with self._conn`` transaction unions the new ids
        in via ``INSERT OR IGNORE`` on the ``version_id`` PRIMARY KEY, so a
        re-recorded id is a silent no-op — matching the append-only set's
        never-duplicated semantics.
        """
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO impact_review_surfaced "
                "(version_id) VALUES (?)",
                [(vid,) for vid in version_ids],
            )

    def seed_impact_review_surfaced_once(self, version_ids: set[str]) -> None:
        """Seed the surfaced set from *version_ids* once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_orphaned_cure_once`: two connections racing their first seed
        over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`) would each
        pass a lock-free marker / emptiness check and both ``INSERT``. Acquiring
        the write lock up front serializes them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either.
        """
        rows = [(vid,) for vid in sorted(version_ids)]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_IMPACT_REVIEW_SURFACED_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM impact_review_surfaced",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO impact_review_surfaced "
                    "(version_id) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_IMPACT_REVIEW_SURFACED_SEEDED_KEY,),
            )

    # -- mergeable-flip serviced-marker ledger ----------------------------

    def is_mergeable_serviced_seeded(self) -> bool:
        """Return whether the one-time mergeable-serviced seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_MERGEABLE_SERVICED_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def is_merge_request_serviced(self, pr_number: int, head_sha: str) -> bool:
        """Return whether ``(pr_number, head_sha)`` is recorded serviced."""
        cur = self._conn.execute(
            "SELECT 1 FROM mergeable_serviced "
            "WHERE pr_number = ? AND head_sha = ?",
            (pr_number, head_sha),
        )
        return cur.fetchone() is not None

    def record_merge_request_serviced(
        self, pr_number: int, head_sha: str,
    ) -> None:
        """Idempotently record ``(pr_number, head_sha)`` as serviced — one txn.

        The SQLite analogue of the JSON adapter's ``file_lock``-guarded
        load-add-write: one ``with self._conn`` transaction unions the pair in via
        ``INSERT OR IGNORE`` on the composite ``(pr_number, head_sha)`` PRIMARY
        KEY, so a re-recorded pair is a silent no-op — matching the append-only
        set's never-duplicated semantics.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO mergeable_serviced "
                "(pr_number, head_sha) VALUES (?, ?)",
                (pr_number, head_sha),
            )

    def seed_mergeable_serviced_once(
        self, serviced: dict[int, set[str]],
    ) -> None:
        """Seed the serviced set from *serviced* once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_impact_review_surfaced_once`: two connections racing their
        first seed over the shared ``state.db`` (see :data:`_BUSY_TIMEOUT_MS`)
        would each pass a lock-free marker / emptiness check and both ``INSERT``.
        Acquiring the write lock up front serializes them: the loser blocks on
        ``busy_timeout``, then re-reads the now-stamped marker inside its own
        transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either.
        """
        rows = [
            (pr, sha) for pr in sorted(serviced) for sha in sorted(serviced[pr])
        ]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_MERGEABLE_SERVICED_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM mergeable_serviced",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO mergeable_serviced "
                    "(pr_number, head_sha) VALUES (?, ?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_MERGEABLE_SERVICED_SEEDED_KEY,),
            )

    # -- conscious-episode ledger -----------------------------------------

    def is_conscious_episodes_seeded(self) -> bool:
        """Return whether the one-time conscious-episode seed marker is stamped."""
        cur = self._conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ?",
            (_CONSCIOUS_EPISODES_SEEDED_KEY,),
        )
        return cur.fetchone() is not None

    def append_conscious_episode(self, record: ConsciousEpisode) -> None:
        """Append one conscious-episode row, auto-assigning its FIFO ``id``.

        The whole record stores as a single
        ``json.dumps(episode_to_dict(record))`` blob in ``data``, matching
        :data:`_CONSCIOUS_EPISODES_DDL` — so additive
        :class:`~maestro.conscious_workspace.ConsciousEpisode` fields never force
        a table migration. The explicit
        :func:`~maestro.conscious_workspace.episode_to_dict` serializer (not
        ``asdict``) is shared with the JSONL adapter so the on-disk shape can
        never drift between the two substrates.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO conscious_episodes (data) VALUES (?)",
                (json.dumps(episode_to_dict(record)),),
            )

    def load_conscious_episodes(self) -> list[ConsciousEpisode]:
        """Return every conscious-episode row oldest-first (``ORDER BY id ASC``).

        The ``AUTOINCREMENT`` ``id`` preserves append order, so an ascending read
        reproduces the JSONL forward (oldest-first) order. Each row's ``data``
        blob is decoded and hydrated through
        :func:`~maestro.conscious_workspace._parse_conscious_episode_dict` — the
        same core the JSONL reader uses — so a blob written by a newer schema
        (extra keys) round-trips through the known fields, and any
        structurally-corrupt blob is dropped (not raised) exactly as the JSONL
        path drops it.
        """
        cur = self._conn.execute(
            "SELECT data FROM conscious_episodes ORDER BY id ASC",
        )
        records: list[ConsciousEpisode] = []
        for row in cur.fetchall():
            record = _parse_conscious_episode_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        return records

    def load_conscious_episodes_tail(self, limit: int) -> list[ConsciousEpisode]:
        """Return the newest *limit* episode rows oldest-first (bounded read).

        ``ORDER BY id DESC LIMIT ?`` reads at most *limit* rows from the tail,
        then reverses into the oldest-first order
        :meth:`load_conscious_episodes` returns, so a caller needing only a short
        tail pays O(limit), not O(history). ``limit <= 0`` returns ``[]``. Each
        blob hydrates through the same
        :func:`~maestro.conscious_workspace._parse_conscious_episode_dict` core,
        so a structurally-corrupt blob is dropped (not raised).
        """
        if limit <= 0:
            return []
        cur = self._conn.execute(
            "SELECT data FROM conscious_episodes ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        records: list[ConsciousEpisode] = []
        for row in cur.fetchall():
            record = _parse_conscious_episode_dict(json.loads(row["data"]))
            if record is not None:
                records.append(record)
        records.reverse()
        return records

    def seed_conscious_episodes_once(
        self, records: list[ConsciousEpisode],
    ) -> None:
        """Seed the conscious-episode table from *records* exactly once, atomically.

        Copies the ``BEGIN IMMEDIATE`` seed-once race guard from
        :meth:`seed_gate_activations_once`: the ledger only appends (no idempotent
        replace and no unique key), so two connections racing their first seed
        over the shared ``state.db`` would each pass a lock-free marker / emptiness
        check and both ``INSERT``, doubling the persisted trace. Acquiring the
        write lock up front with ``BEGIN IMMEDIATE`` serialises them: the loser
        blocks on ``busy_timeout``, then re-reads the now-stamped marker inside its
        own transaction and skips. The insert stays guarded on an empty table so a
        store populated by a prior run that crashed before stamping the marker is
        never doubled either.

        *records* is oldest-first (see
        :func:`~maestro.conscious_workspace.load_all_conscious_episodes`), so the
        ``AUTOINCREMENT`` ``id`` reproduces the append order and a later
        ``ORDER BY id ASC`` read matches the JSONL forward order.
        """
        rows = [(json.dumps(episode_to_dict(r)),) for r in records]
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            already = self._conn.execute(
                "SELECT 1 FROM store_metadata WHERE key = ?",
                (_CONSCIOUS_EPISODES_SEEDED_KEY,),
            ).fetchone()
            if already is not None:
                return
            count_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM conscious_episodes",
            ).fetchone()
            if rows and int(count_row["n"]) == 0:
                self._conn.executemany(
                    "INSERT INTO conscious_episodes (data) VALUES (?)",
                    rows,
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata (key, value) "
                "VALUES (?, '1')",
                (_CONSCIOUS_EPISODES_SEEDED_KEY,),
            )

    # -- eager whole-fleet migration ---------------------------------------

    @staticmethod
    def _seed_sized(
        items: _SeedArg,
        seed: Callable[[_SeedArg], None],
    ) -> int:
        """Seed *items* via *seed* and return the row count for the summary.

        The shared body for every standard registry entry whose legacy loader
        returns a sized collection (``list`` / ``dict`` / ``set``): the inferred
        :data:`_SeedArg` binds the loader's return type to the seed's argument
        type, so a mismatched pairing is a type error rather than a run-time
        surprise. The count is the loaded row count, which equals the rows the
        underlying ``seed_*_once`` inserts on the normal empty-table path.
        """
        seed(items)
        return len(items)

    def _migrate_task_queue(self, memory_dir: Path) -> int:
        """Seed the task queue from ``task_queue.json`` and stamp its marker.

        The task queue is the one store without a ``load_all_*`` reader and
        without a ``seed_*_once`` shape, so its migration is spelled out here:
        load via :class:`~maestro.task_queue.TaskQueue`, replace, then stamp.
        """
        items = TaskQueue(memory_dir / "task_queue.json").load()
        self.replace_task_queue(items)
        self.mark_task_queue_seeded()
        return len(items)

    def _migrate_remediation_tracker(self, memory_dir: Path) -> int:
        """Seed the remediation tracker from its whole-state legacy payload.

        Unlike the collection-shaped stores, the tracker's source is a single
        :class:`~maestro.remediation_tracker.TrackerState` (carrying its
        ``schema_version``); the reported count is the number of per-PR rows the
        seed projects onto the keyed table, matching ``load_remediation_tracker``.
        """
        state = load_tracker_state(memory_dir)
        self.seed_remediation_tracker_once(state)
        return len(_remediation_tracker_rows(state))

    def _migration_entries(self, memory_dir: Path) -> list[_MigrationEntry]:
        """Return the explicit per-store migration registry (proposal 106).

        One entry per seedable store, listed explicitly rather than derived by
        reflection because the seed method names diverge from their predicates
        (``seed_backlog_snapshots_once`` ↔ ``is_owned_backlog_seeded``) and the
        seed argument types are heterogeneous. The two non-``seed_*_once`` shapes
        — the task queue and the remediation tracker — carry bespoke closures.
        Cost samples are deliberately absent (no seed by design). Every ``seed``
        closure defers its ``load_all_*`` read until invoked, so an
        already-seeded store never reads its legacy file.
        """
        return [
            _MigrationEntry(
                "task_queue",
                self.is_task_queue_seeded,
                lambda: self._migrate_task_queue(memory_dir),
            ),
            _MigrationEntry(
                "deed_ledger",
                self.is_deed_ledger_seeded,
                lambda: self._seed_sized(
                    load_all_deeds(memory_dir), self.seed_deeds_once,
                ),
            ),
            _MigrationEntry(
                "lever_attempts",
                self.is_lever_attempts_seeded,
                lambda: self._seed_sized(
                    load_all_lever_attempts(memory_dir),
                    self.seed_lever_attempts_once,
                ),
            ),
            _MigrationEntry(
                "audit_history",
                self.is_audit_history_seeded,
                lambda: self._seed_sized(
                    load_all_audit_history(memory_dir),
                    self.seed_audit_history_once,
                ),
            ),
            _MigrationEntry(
                "rejection_history",
                self.is_rejection_history_seeded,
                lambda: self._seed_sized(
                    load_all_rejection_history(memory_dir),
                    self.seed_rejection_history_once,
                ),
            ),
            _MigrationEntry(
                "version_archive",
                self.is_version_archive_seeded,
                lambda: self._seed_sized(
                    load_all_versions(memory_dir),
                    self.seed_version_archive_once,
                ),
            ),
            _MigrationEntry(
                "pr_ledger",
                self.is_pr_ledger_seeded,
                lambda: self._seed_sized(
                    load_all_pr_ledger_entries(memory_dir),
                    self.seed_pr_ledger_once,
                ),
            ),
            _MigrationEntry(
                "remediation_tracker",
                self.is_remediation_tracker_seeded,
                lambda: self._migrate_remediation_tracker(memory_dir),
            ),
            _MigrationEntry(
                "escalation_standdowns",
                self.is_escalation_standdowns_seeded,
                lambda: self._seed_sized(
                    load_all_escalation_standdowns(memory_dir),
                    self.seed_escalation_standdowns_once,
                ),
            ),
            _MigrationEntry(
                "question_resolutions",
                self.is_question_resolutions_seeded,
                lambda: self._seed_sized(
                    load_all_question_resolutions(memory_dir),
                    self.seed_question_resolutions_once,
                ),
            ),
            _MigrationEntry(
                "gate_activations",
                self.is_gate_activations_seeded,
                lambda: self._seed_sized(
                    load_all_gate_activations(memory_dir),
                    self.seed_gate_activations_once,
                ),
            ),
            _MigrationEntry(
                "premise_observations",
                self.is_premise_observations_seeded,
                lambda: self._seed_sized(
                    load_all_premise_observations(memory_dir),
                    self.seed_premise_observations_once,
                ),
            ),
            _MigrationEntry(
                "predictions",
                self.is_predictions_seeded,
                lambda: self._seed_sized(
                    load_all_predictions(memory_dir),
                    self.seed_predictions_once,
                ),
            ),
            _MigrationEntry(
                "owned_backlog",
                self.is_owned_backlog_seeded,
                lambda: self._seed_sized(
                    load_all_backlog_snapshots(memory_dir),
                    self.seed_backlog_snapshots_once,
                ),
            ),
            _MigrationEntry(
                "pr_provenance",
                self.is_pr_provenance_seeded,
                lambda: self._seed_sized(
                    load_all_provenance_records(memory_dir),
                    self.seed_provenance_once,
                ),
            ),
            _MigrationEntry(
                "settlement_reconciliations",
                self.is_settlement_reconciliations_seeded,
                lambda: self._seed_sized(
                    load_all_settlement_reconciliations(memory_dir),
                    self.seed_settlement_reconciliations_once,
                ),
            ),
            _MigrationEntry(
                "obligation_fork",
                self.is_obligation_fork_seeded,
                lambda: self._seed_sized(
                    load_all_obligation_forks(memory_dir),
                    self.seed_obligation_forks_once,
                ),
            ),
            _MigrationEntry(
                "fetch_truth",
                self.is_fetch_truth_seeded,
                lambda: self._seed_sized(
                    load_all_fetch_truth_markers(memory_dir),
                    self.seed_fetch_truth_once,
                ),
            ),
            _MigrationEntry(
                "preflight_passes",
                self.is_preflight_passes_seeded,
                lambda: self._seed_sized(
                    load_all_preflight_passes(memory_dir),
                    self.seed_preflight_passes_once,
                ),
            ),
            _MigrationEntry(
                "dropped_directives",
                self.is_dropped_directives_seeded,
                lambda: self._seed_sized(
                    load_all_dropped_directives(memory_dir),
                    self.seed_dropped_directives_once,
                ),
            ),
            _MigrationEntry(
                "settled_verdicts",
                self.is_settled_verdicts_seeded,
                lambda: self._seed_sized(
                    load_all_settled_verdicts(memory_dir),
                    self.seed_settled_verdicts_once,
                ),
            ),
            _MigrationEntry(
                "stand_down_verdicts",
                self.is_stand_down_verdicts_seeded,
                lambda: self._seed_sized(
                    load_all_stand_down_records(memory_dir),
                    self.seed_stand_down_verdicts_once,
                ),
            ),
            _MigrationEntry(
                "unconfirmed_landing",
                self.is_unconfirmed_landing_seeded,
                lambda: self._seed_sized(
                    load_all_unconfirmed_landing_seen(memory_dir),
                    self.seed_unconfirmed_landing_once,
                ),
            ),
            _MigrationEntry(
                "orphaned_cure",
                self.is_orphaned_cure_seeded,
                lambda: self._seed_sized(
                    load_all_orphaned_cure_seen(memory_dir),
                    self.seed_orphaned_cure_once,
                ),
            ),
            _MigrationEntry(
                "impact_review_surfaced",
                self.is_impact_review_surfaced_seeded,
                lambda: self._seed_sized(
                    load_all_impact_review_surfaced(memory_dir),
                    self.seed_impact_review_surfaced_once,
                ),
            ),
            _MigrationEntry(
                "mergeable_serviced",
                self.is_mergeable_serviced_seeded,
                lambda: self._seed_sized(
                    load_all_serviced_merge_requests(memory_dir),
                    self.seed_mergeable_serviced_once,
                ),
            ),
            _MigrationEntry(
                "conscious_episodes",
                self.is_conscious_episodes_seeded,
                lambda: self._seed_sized(
                    load_all_conscious_episodes(memory_dir),
                    self.seed_conscious_episodes_once,
                ),
            ),
        ]

    def migrate_from_legacy(self, memory_dir: Path) -> StateMigrationSummary:
        """Eagerly seed every legacy sidecar under *memory_dir* into ``state.db``.

        The single deterministic migration point: at loop startup under
        ``[paths] sqlite_state_enabled`` this walks the explicit per-store
        registry once, seeding each store that is not yet seeded and skipping
        each that already carries its durable marker. It is idempotent and
        composes with the lazy per-adapter path — the ``BEGIN IMMEDIATE`` +
        empty-table guards inside every ``seed_*_once`` make a second (lazy or
        eager) seed a no-op — so a table an adapter seeded during construction
        is reported under ``already_seeded`` rather than re-counted, keeping the
        summary honest. Cost samples are deliberately never seeded (the burn-rate
        window self-heals and ``cost_samples.jsonl`` is the flag-off rollback).
        Returns a :class:`StateMigrationSummary` for the caller to log.
        """
        seeded: dict[str, int] = {}
        already_seeded: list[str] = []
        for entry in self._migration_entries(memory_dir):
            if entry.is_seeded():
                already_seeded.append(entry.store_name)
            else:
                seeded[entry.store_name] = entry.seed()
        return StateMigrationSummary(
            seeded=seeded,
            already_seeded=already_seeded,
        )


class SqliteTaskQueue:
    """SQLite-backed :class:`~maestro.task_queue.TaskQueueBackend`.

    Wraps a :class:`StateStore`, delegating the four backend methods to the
    store's task-queue operations. On first access, if the store has never been
    seeded and a legacy ``task_queue.json`` exists, it seeds the store once from
    ``TaskQueue(legacy_json_path).load()`` (reusing the JSON backend's defensive
    cleaning) so enabling the flag does not drop a pending queue. The seed fires
    at most once per DB lifetime — a *durable* marker (``is_task_queue_seeded``),
    not a transient "store is currently empty" check, guards it, so a queue that
    legitimately drains and then restarts is never re-seeded from the frozen
    legacy JSON (which would resurrect already-consumed tasks; the JSON backend
    returns ``[]`` once drained).

    ``save`` runs each item through :func:`~maestro.task_queue.normalize_task`
    before persisting, so the store never holds dirty rows and every read path
    (``load`` / ``head`` / ``pop_head``) matches the JSON backend's clean-on-read
    output — the JSON backend cleans on read, this one cleans on write.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_path: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_path = legacy_json_path
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard. The in-memory ``_seed_checked`` flag
        # does not survive a process restart, and an "is the store empty?"
        # heuristic would resurrect already-consumed tasks from the frozen
        # legacy JSON once the queue legitimately drains. A persisted marker
        # fires the seed at most once ever, matching the JSON backend (which
        # returns ``[]`` once drained). Seed first, then mark: a crash between
        # the two leaves the store populated, so the next start's "already
        # populated" check skips the re-seed before stamping the marker.
        if self._store.is_task_queue_seeded():
            return
        if (
            self._legacy_json_path is not None
            and not self._store.load_task_queue()
            and self._legacy_json_path.exists()
        ):
            legacy = TaskQueue(self._legacy_json_path).load()
            if legacy:
                self._store.replace_task_queue(legacy)
        self._store.mark_task_queue_seeded()

    def load(self) -> list[QueuedTask]:
        self._maybe_seed()
        return self._store.load_task_queue()

    def save(self, items: list[QueuedTask]) -> None:
        self._maybe_seed()
        # Clean on write (the JSON backend cleans on read) so the store never
        # holds dirty rows and all read paths are substrate-parity-clean.
        cleaned = [
            task
            for task in (normalize_task(item) for item in items)
            if task is not None
        ]
        self._store.replace_task_queue(cleaned)

    def head(self) -> QueuedTask | None:
        self._maybe_seed()
        items = self._store.load_task_queue()
        return items[0] if items else None

    def pop_head(self) -> QueuedTask | None:
        self._maybe_seed()
        return self._store.pop_task_queue_head()

    def mutate(self, fn: Callable[[list[QueuedTask]], T]) -> T:
        """Serialised whole-queue read-modify-write (see
        :meth:`StateStore.mutate_task_queue`).

        The clean-on-write step :meth:`save` applies is applied here too, over
        the list *fn* leaves behind, so the mutate path cannot slip a dirty row
        past the substrate-parity guarantee.
        """
        self._maybe_seed()

        def _clean_then_apply(items: list[QueuedTask]) -> T:
            result = fn(items)
            cleaned = [
                task
                for task in (normalize_task(item) for item in items)
                if task is not None
            ]
            items[:] = cleaned
            return result

        return self._store.mutate_task_queue(_clean_then_apply)


class SqliteCostSampleStore:
    """SQLite-backed :class:`~maestro.cost_tracking.CostSampleBackend`.

    Adapts a :class:`StateStore`'s cost-sample methods to the backend surface
    the SDK write path and the windowed / recent-N readers consume, mirroring
    :class:`SqliteTaskQueue`. Because the backend Protocol is
    ``@runtime_checkable`` this satisfies it structurally, so no import of the
    Protocol is needed here (which would also invert the module import edge).

    Unlike the task-queue adapter there is no legacy-JSON seed: with the flag on
    the burn-rate window simply starts cold and self-heals within one
    ``budget_rate_window_hours``, and ``cost_samples.jsonl`` is retained
    untouched as the flag-off rollback (proposal 106, "Out of scope").
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def append(self, sample: CostSample) -> None:
        self._store.append_cost_sample(sample)

    def samples_in_window(
        self, window_hours: float, *, now: datetime | None = None,
    ) -> list[CostSample]:
        reference = now or datetime.now(UTC)
        cutoff_epoch = reference.timestamp() - window_hours * 3600
        return self._store.load_cost_samples_since(cutoff_epoch)

    def recent_samples(self, limit: int) -> list[CostSample]:
        return self._store.recent_cost_samples(limit)

    def cache_key(self) -> tuple[str, int | None]:
        # ``id(self)`` namespaces the module-level TTL cache per adapter
        # instance, so two live backends can never collide on a shared key; the
        # long-lived production adapter holds one stable identity for its
        # lifetime. The version is the max sample id — advanced by every append,
        # the SQLite analogue of the JSONL backend's file mtime.
        return (
            f"sqlite-cost-samples:{id(self)}",
            self._store.max_cost_sample_id(),
        )


class SqliteJournalSearchIndex:
    """SQLite-backed :class:`~maestro.search.JournalSearchBackend` (proposal 106).

    Adapts a :class:`StateStore`'s persistent ``journal_search`` FTS5 index to
    the injectable ranker seam :meth:`~maestro.journal.Journal.search` routes
    through, mirroring :class:`SqliteCostSampleStore`. Because
    :class:`~maestro.search.JournalSearchBackend` is ``@runtime_checkable`` this
    satisfies it structurally, so no import of the Protocol is needed here (which
    would also invert the module import edge).

    Unlike the seeded ledgers there is no legacy seed, no durable
    ``is_*_seeded`` marker, and no migration-fleet registration: this is a
    *rebuildable derived cache*, a self-healing projection of the append-only
    ``journal.jsonl`` the ``corpus`` argument is built from. With the flag on the
    index simply starts cold and reconciles itself from the corpus on the first
    search; ``journal.jsonl`` stays the sole source of truth and the flag-off
    rollback (proposal 106, "Out of scope"), exactly as ``cost_samples.jsonl`` is
    for :class:`SqliteCostSampleStore`.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def rank(self, corpus: Sequence[str], query: str) -> list[int]:
        return self._store.rank_journal_search(corpus, query)


class SqliteDeedLedger:
    """SQLite-backed :class:`~maestro.deed_ledger.DeedLedgerBackend`.

    Wraps a :class:`StateStore`, delegating the three backend methods to the
    store's deed operations, mirroring :class:`SqliteTaskQueue`. On first access,
    if the store has never been seeded and a legacy ``deed_ledger.jsonl`` exists,
    it seeds the store once from :func:`~maestro.deed_ledger.load_all_deeds` so
    enabling the flag preserves the all-time deed count. The seed fires at most
    once per DB lifetime — a *durable* marker (``is_deed_ledger_seeded``), not a
    transient "store is empty" check, guards it, so a ledger that legitimately
    drains and restarts is never re-seeded from the frozen legacy JSONL. Seed
    first, then mark: a crash between the two leaves the store populated, so the
    next start's "already populated" check skips the re-seed before stamping the
    marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained ledger is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start (it avoids taking a
        # write lock every process launch); ``seed_deeds_once`` re-checks the
        # marker while holding the ``BEGIN IMMEDIATE`` write lock, so two
        # processes racing their very first seed over the shared ``state.db``
        # cannot both insert (which would double the all-time count). Seed
        # first, then mark — both inside that one atomic transaction.
        if self._store.is_deed_ledger_seeded():
            return
        legacy = (
            load_all_deeds(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_deeds_once(legacy)

    def record(
        self,
        deed_type: DeedType | str,
        pr_number: int | None,
        detail: str,
        *,
        now: str | None = None,
    ) -> Deed:
        self._maybe_seed()
        deed = build_deed(deed_type, pr_number, detail, now=now)
        self._store.append_deed(deed)
        return deed

    def count(self, *, since: str | None = None) -> int:
        self._maybe_seed()
        return self._store.count_deeds_since(since)

    def recent(self, n: int = 20) -> list[Deed]:
        self._maybe_seed()
        return self._store.recent_deeds(n)


class SqliteLeverAttemptsLedger:
    """SQLite-backed :class:`~maestro.lever_capture.LeverAttemptsBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's lever-attempts operations, mirroring :class:`SqliteDeedLedger`. On
    first access, if the store has never been seeded and a legacy
    ``lever_attempts.jsonl`` exists, it seeds the store once from
    :func:`~maestro.lever_capture.load_all_lever_attempts` so enabling the flag
    preserves the distinct-``True`` count the selector scores on. The seed fires
    at most once per DB lifetime — a *durable* marker (``is_lever_attempts
    _seeded``), not a transient "store is empty" check, guards it, so a ledger
    that legitimately drains and restarts is never re-seeded from the frozen
    legacy JSONL. Seed first, then mark: a crash between the two leaves the store
    populated, so the next start's "already populated" check skips the re-seed
    before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained ledger is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_lever_attempts_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_lever_attempts_seeded():
            return
        legacy = (
            load_all_lever_attempts(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_lever_attempts_once(legacy)

    def record(self, attempt: LeverAttempt) -> None:
        self._maybe_seed()
        self._store.append_lever_attempt(attempt)

    def load_all(self) -> list[LeverAttempt]:
        self._maybe_seed()
        return self._store.load_lever_attempts()


class SqliteAuditHistoryLedger:
    """SQLite-backed :class:`~maestro.memory.AuditHistoryBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's audit-history operations, mirroring :class:`SqliteLeverAttemptsLedger`.
    On first access, if the store has never been seeded and a legacy
    ``audit_history.jsonl`` exists, it seeds the store once from
    :func:`~maestro.memory.load_all_audit_history` so enabling the flag
    preserves all-time audit history. The seed fires at most once per DB
    lifetime — a *durable* marker (``is_audit_history_seeded``), not a transient
    "store is empty" check, guards it, so a history that legitimately drains and
    restarts is never re-seeded from the frozen legacy JSONL. Seed first, then
    mark: a crash between the two leaves the store populated, so the next start's
    "already populated" check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained history is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_audit_history_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_audit_history_seeded():
            return
        legacy = (
            load_all_audit_history(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_audit_history_once(legacy)

    def record(self, result: AuditResult) -> None:
        self._maybe_seed()
        self._store.append_audit_result(result)

    def load_all(self) -> list[AuditResult]:
        self._maybe_seed()
        return self._store.load_audit_results()


class SqliteRejectionHistoryLedger:
    """SQLite-backed :class:`~maestro.memory.RejectionHistoryBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's rejection-history operations, mirroring
    :class:`SqliteAuditHistoryLedger`. On first access, if the store has never
    been seeded and a legacy ``rejection_history.jsonl`` exists, it seeds the
    store once from :func:`~maestro.memory.load_all_rejection_history` so enabling
    the flag preserves all-time rejection history. The seed fires at most once
    per DB lifetime — a *durable* marker (``is_rejection_history_seeded``), not a
    transient "store is empty" check, guards it, so a history that legitimately
    drains and restarts is never re-seeded from the frozen legacy JSONL. Seed
    first, then mark: a crash between the two leaves the store populated, so the
    next start's "already populated" check skips the re-seed before stamping the
    marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained history is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_rejection_history_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_rejection_history_seeded():
            return
        legacy = (
            load_all_rejection_history(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_rejection_history_once(legacy)

    def record(self, record: RejectionRecord) -> None:
        self._maybe_seed()
        self._store.append_rejection(record)

    def load_all(self) -> list[RejectionRecord]:
        self._maybe_seed()
        return self._store.load_rejections()


class SqliteVersionArchiveLedger:
    """SQLite-backed :class:`~maestro.memory.VersionArchiveBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's version-archive operations, mirroring
    :class:`SqliteRejectionHistoryLedger`. On first access, if the store has
    never been seeded and a legacy ``version_archive.jsonl`` exists, it seeds the
    store once from :func:`~maestro.memory.load_all_versions` so enabling the flag
    preserves the all-time merged-plan ledger the three readers (``Memory``, the
    owned-act-drought sensor, the merge-truth corroboration reader) walk. The
    seed fires at most once per DB lifetime — a *durable* marker
    (``is_version_archive_seeded``), not a transient "store is empty" check,
    guards it, so an archive that legitimately drains and restarts is never
    re-seeded from the frozen legacy JSONL. Seed first, then mark: a crash
    between the two leaves the store populated, so the next start's "already
    populated" check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained archive is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_version_archive_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_version_archive_seeded():
            return
        legacy = (
            load_all_versions(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_version_archive_once(legacy)

    def record(self, record: VersionRecord) -> None:
        self._maybe_seed()
        self._store.append_version(record)

    def load_all(self) -> list[VersionRecord]:
        self._maybe_seed()
        return self._store.load_versions()


class SqlitePRLedger:
    """SQLite-backed :class:`~maestro.pr_ledger.PRLedgerBackend`.

    Wraps a :class:`StateStore`, delegating :meth:`read` to the store's whole-map
    load and :meth:`mutate` to a load → *fn* → change-detected keyed-replace run
    inside one ``BEGIN IMMEDIATE`` write transaction
    (:meth:`StateStore.mutate_pr_ledger`). On first access, if the store
    has never been seeded and a legacy ``pr_ledger.json`` exists, it seeds the
    store once from :func:`~maestro.pr_ledger.load_all_pr_ledger_entries` so
    enabling the flag preserves the in-flight per-PR budget/latch state across
    the flip. The seed fires at most once per DB lifetime — a *durable* marker
    (``is_pr_ledger_seeded``), not a transient "store is empty" check, guards it,
    so a ledger whose entries legitimately evict / prune to empty is never
    re-seeded from the frozen legacy JSON. Seed first, then mark: a crash between
    the two leaves the store populated, so the next start's "already populated"
    check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_dir = legacy_json_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so an emptied ledger is never
        # re-seeded from the frozen legacy JSON. This lock-free read is only a
        # fast path for the common already-seeded start; ``seed_pr_ledger_once``
        # re-checks the marker while holding the ``BEGIN IMMEDIATE`` write lock,
        # so two processes racing their very first seed over the shared
        # ``state.db`` cannot both insert.
        if self._store.is_pr_ledger_seeded():
            return
        legacy = (
            load_all_pr_ledger_entries(self._legacy_json_dir)
            if self._legacy_json_dir is not None
            else []
        )
        self._store.seed_pr_ledger_once(legacy)

    def read(self) -> dict[int, PRLedgerEntry]:
        self._maybe_seed()
        return self._store.load_pr_ledger()

    def mutate(self, fn: Callable[[dict[int, PRLedgerEntry]], T]) -> T:
        """Load → *fn* → keyed-replace, persisting iff *fn* changed the map.

        Delegates to :meth:`StateStore.mutate_pr_ledger`, which runs the whole
        read-modify-write inside one ``BEGIN IMMEDIATE`` transaction — atomic
        across the shared ``state.db`` connections, matching the
        :func:`~maestro.fileutil.file_lock` span of
        :meth:`~maestro.pr_ledger.JsonPRLedger.mutate`. The change guard also
        mirrors the JSON store: a no-op closure (a missing entry, nothing to
        reset, no stale rows) leaves the map unchanged, so no rewrite runs and
        the "quiet no-op writes nothing" contract holds on SQLite too.
        """
        self._maybe_seed()
        return self._store.mutate_pr_ledger(fn)


class SqliteRemediationTrackerStore:
    """SQLite-backed :class:`~maestro.remediation_tracker.RemediationTrackerBackend`.

    Wraps a :class:`StateStore`, delegating :meth:`load` to the store's
    whole-state re-assembly and :meth:`persist` to its one-transaction keyed
    replace (:meth:`StateStore.replace_remediation_tracker`) — the substrate
    analogue of the JSON path's whole-file
    :func:`~maestro.fileutil.atomic_write`. On first access, if the store has
    never been seeded and a legacy ``remediation_tracker.json`` exists, it seeds
    the store once from
    :func:`~maestro.remediation_tracker.load_tracker_state` so enabling the flag
    preserves the in-flight re-route-gate state — the no-op streaks, item
    baselines, and skip counts — across the flip, along with the stored schema
    version so a pre-#2796 sidecar still triggers its one-time merge-block
    reconciliation on the new substrate. The legacy JSON is read, never written
    or removed: it stays the flag-off rollback. The seed fires at most once per
    DB lifetime — a *durable* marker (``is_remediation_tracker_seeded``), not a
    transient "store is empty" check, guards it, so gate state that legitimately
    evicts to empty is never re-seeded from the frozen legacy JSON. Seed first,
    then mark: a crash between the two leaves the store populated, so the next
    start's "already populated" check skips the re-seed before stamping the
    marker. Mirrors :class:`SqlitePRLedger`.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_dir = legacy_json_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so an emptied tracker is
        # never re-seeded from the frozen legacy JSON. This lock-free read is
        # only a fast path for the common already-seeded start;
        # ``seed_remediation_tracker_once`` re-checks the marker while holding
        # the ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_remediation_tracker_seeded():
            return
        legacy = (
            load_tracker_state(self._legacy_json_dir)
            if self._legacy_json_dir is not None
            else TrackerState({}, {}, {}, {}, 0)
        )
        self._store.seed_remediation_tracker_once(legacy)

    def load(self) -> TrackerState:
        self._maybe_seed()
        return self._store.load_remediation_tracker()

    def persist(self, state: TrackerState) -> None:
        self._maybe_seed()
        self._store.replace_remediation_tracker(state)


class SqliteEscalationLedger:
    """SQLite-backed :class:`~maestro.escalation_bridge.EscalationLedgerBackend`.

    Wraps a :class:`StateStore`, delegating :meth:`read` to the store's whole-map
    load and :meth:`mutate` to a load → *fn* → change-detected keyed-replace run
    inside one ``BEGIN IMMEDIATE`` write transaction
    (:meth:`StateStore.mutate_escalation_standdowns`). On first access, if the
    store has never been seeded and a legacy ``escalation_handoffs.json`` exists,
    it seeds the store once from
    :func:`~maestro.escalation_bridge.load_all_escalation_standdowns` so enabling
    the flag preserves the in-flight escalation standdowns — the durable "handed
    up to Thomas — stop minting remediation laps" records, each with its serviced
    carve-out set and optional expiry — across the flip, with the pre-080
    ``"<pr>@<head>"`` keys collapsed to canonical ``number`` keys at seed time.
    The legacy JSON is read, never written or removed: it stays the flag-off
    rollback. The seed fires at most once per DB lifetime — a *durable* marker
    (``is_escalation_standdowns_seeded``), not a transient "store is empty" check,
    guards it, so a ledger whose standdowns legitimately clear / prune to empty is
    never re-seeded from the frozen legacy JSON. Seed first, then mark: a crash
    between the two leaves the store populated, so the next start's "already
    populated" check skips the re-seed before stamping the marker. Mirrors
    :class:`SqlitePRLedger`.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_dir = legacy_json_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so an emptied ledger is never
        # re-seeded from the frozen legacy JSON. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_escalation_standdowns_once`` re-checks the marker while holding
        # the ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_escalation_standdowns_seeded():
            return
        legacy = (
            load_all_escalation_standdowns(self._legacy_json_dir)
            if self._legacy_json_dir is not None
            else {}
        )
        self._store.seed_escalation_standdowns_once(legacy)

    def read(self) -> dict[int, StanddownRecord]:
        self._maybe_seed()
        return self._store.load_escalation_standdowns()

    def mutate(self, fn: Callable[[dict[int, StanddownRecord]], T]) -> T:
        """Load → *fn* → keyed-replace, persisting iff *fn* changed the map.

        Delegates to :meth:`StateStore.mutate_escalation_standdowns`, which runs
        the whole read-modify-write inside one ``BEGIN IMMEDIATE`` transaction —
        atomic across the shared ``state.db`` connections, matching the
        :func:`~maestro.fileutil.file_lock` span of
        :meth:`~maestro.escalation_bridge.JsonEscalationLedger.mutate`. The change
        guard also mirrors the JSON store: a no-op closure (e.g. a union of
        already-serviced ids, or the ``deliver_unwinnable_race`` deliverer that
        records no standdown) leaves the map unchanged, so no rewrite runs and the
        "quiet no-op writes nothing" contract holds on SQLite too.
        """
        self._maybe_seed()
        return self._store.mutate_escalation_standdowns(fn)


class SqliteQuestionResolutionLedger:
    """SQLite-backed :class:`~maestro.memory.QuestionResolutionBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's question-resolution operations, mirroring
    :class:`SqliteRejectionHistoryLedger`. On first access, if the store has
    never been seeded and a legacy ``question_resolutions.jsonl`` exists, it seeds
    the store once from
    :func:`~maestro.memory.load_all_question_resolutions` so enabling the flag
    preserves the all-time lineage the stalled-question audit rule walks. The seed
    fires at most once per DB lifetime — a *durable* marker
    (``is_question_resolutions_seeded``), not a transient "store is empty" check,
    guards it, so a history that legitimately drains and restarts is never
    re-seeded from the frozen legacy JSONL. Seed first, then mark: a crash between
    the two leaves the store populated, so the next start's "already populated"
    check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained history is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_question_resolutions_once`` re-checks the marker while holding
        # the ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_question_resolutions_seeded():
            return
        legacy = (
            load_all_question_resolutions(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_question_resolutions_once(legacy)

    def record(self, record: QuestionResolution) -> None:
        self._maybe_seed()
        self._store.append_question_resolution(record)

    def load_all(self) -> list[QuestionResolution]:
        self._maybe_seed()
        return self._store.load_question_resolutions()


class SqliteGateActivationLedger:
    """SQLite-backed :class:`~maestro.audit_checks.GateActivationBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's gate-activation operations, mirroring
    :class:`SqliteQuestionResolutionLedger`. On first access, if the store has
    never been seeded and a legacy ``gate_activations.jsonl`` exists, it seeds the
    store once from :func:`~maestro.audit_checks.load_all_gate_activations` so
    enabling the flag preserves the all-time trace the gate-fire-rate sensors and
    CLI diagnostics read. The seed fires at most once per DB lifetime — a
    *durable* marker (``is_gate_activations_seeded``), not a transient "store is
    empty" check, guards it, so a trace that legitimately drains and restarts is
    never re-seeded from the frozen legacy JSONL. Seed first, then mark: a crash
    between the two leaves the store populated, so the next start's "already
    populated" check skips the re-seed before stamping the marker.

    Unlike the sibling question-resolution / rejection ledgers this adapter's
    methods are :meth:`append` / :meth:`read_all` (not ``record`` / ``load_all``)
    to satisfy the append-only trace surface the readers already expect.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained trace is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_gate_activations_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_gate_activations_seeded():
            return
        legacy = (
            load_all_gate_activations(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_gate_activations_once(legacy)

    def append(self, record: GateActivation) -> None:
        self._maybe_seed()
        self._store.append_gate_activation(record)

    def read_all(self) -> list[GateActivation]:
        self._maybe_seed()
        return self._store.load_gate_activations()

    def read_tail(self, limit: int) -> list[GateActivation]:
        self._maybe_seed()
        return self._store.load_gate_activations_tail(limit)

    def read_gate_tail(self, gate: str, limit: int) -> list[GateActivation]:
        self._maybe_seed()
        return self._store.load_gate_activations_tail_for_gate(gate, limit)


class SqliteConsciousEpisodeLedger:
    """SQLite-backed :class:`~maestro.conscious_workspace.ConsciousEpisodeBackend`.

    Wraps a :class:`StateStore`, delegating the three backend methods to the
    store's conscious-episode operations, mirroring
    :class:`SqliteGateActivationLedger`. On first access, if the store has never
    been seeded and a legacy ``conscious_episodes.jsonl`` exists, it seeds the
    store once from
    :func:`~maestro.conscious_workspace.load_all_conscious_episodes` so enabling
    the flag preserves the accumulated trace. The seed fires at most once per DB
    lifetime — a *durable* marker (``is_conscious_episodes_seeded``), not a
    transient "store is empty" check, guards it, so a trace that legitimately
    drains and restarts is never re-seeded from the frozen legacy JSONL. Seed
    first, then mark: a crash between the two leaves the store populated, so the
    next start's "already populated" check skips the re-seed before stamping the
    marker.

    The seed path is load-bearing even though nothing writes an episode yet
    (proposal 130 lands the schema first): JSONL is the flag-off default, so an
    operator who later enables ``[paths] sqlite_state_enabled`` must not lose the
    accumulated trace.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained trace is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_conscious_episodes_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very first
        # seed over the shared ``state.db`` cannot both insert.
        if self._store.is_conscious_episodes_seeded():
            return
        legacy = (
            load_all_conscious_episodes(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_conscious_episodes_once(legacy)

    def append(self, episode: ConsciousEpisode) -> None:
        self._maybe_seed()
        self._store.append_conscious_episode(episode)

    def read_recent(self, n: int) -> list[ConsciousEpisode]:
        self._maybe_seed()
        # The store tail is oldest-first; the backend contract is newest-first
        # (matching the JSONL adapter), so reverse it.
        return list(reversed(self._store.load_conscious_episodes_tail(n)))

    def read_all(self) -> list[ConsciousEpisode]:
        self._maybe_seed()
        return self._store.load_conscious_episodes()


class SqlitePremiseLedger:
    """SQLite-backed :class:`~maestro.audit_checks_calibration.PremiseBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's premise-observation operations, mirroring
    :class:`SqliteGateActivationLedger`. On first access, if the store has never
    been seeded and a legacy ``premise_observations.jsonl`` exists, it seeds the
    store once from
    :func:`~maestro.audit_checks_calibration.load_all_premise_observations` so enabling the
    flag preserves the all-time trace the ``measure-premise-accuracy`` CLI reads.
    The seed fires at most once per DB lifetime — a *durable* marker
    (``is_premise_observations_seeded``), not a transient "store is empty" check,
    guards it, so a trace that legitimately drains and restarts is never
    re-seeded from the frozen legacy JSONL. Seed first, then mark: a crash
    between the two leaves the store populated, so the next start's "already
    populated" check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained trace is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_premise_observations_once`` re-checks the marker while holding
        # the ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_premise_observations_seeded():
            return
        legacy = (
            load_all_premise_observations(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_premise_observations_once(legacy)

    def append(self, record: PremiseObservation) -> None:
        self._maybe_seed()
        self._store.append_premise_observation(record)

    def read_all(self) -> list[PremiseObservation]:
        self._maybe_seed()
        return self._store.load_premise_observations()


class SqlitePredictionLedger:
    """SQLite-backed :class:`~maestro.audit_checks_calibration.PredictionBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's prediction operations, mirroring :class:`SqliteGateActivationLedger`.
    On first access, if the store has never been seeded and a legacy
    ``predictions.jsonl`` exists, it seeds the store once from
    :func:`~maestro.audit_checks_calibration.load_all_predictions` so enabling the flag
    preserves the all-time trace the prediction-accuracy / fork-authorship
    sensors read. The seed fires at most once per DB lifetime — a *durable*
    marker (``is_predictions_seeded``), not a transient "store is empty" check,
    guards it, so a trace that legitimately drains and restarts is never
    re-seeded from the frozen legacy JSONL. Seed first, then mark: a crash
    between the two leaves the store populated, so the next start's "already
    populated" check skips the re-seed before stamping the marker.

    Like :class:`SqliteGateActivationLedger` this adapter's methods are
    :meth:`append` / :meth:`read_all` (not ``record`` / ``load_all``) to satisfy
    the append-only trace surface the readers already expect.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained trace is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start; ``seed_predictions_once``
        # re-checks the marker while holding the ``BEGIN IMMEDIATE`` write lock,
        # so two processes racing their very first seed over the shared
        # ``state.db`` cannot both insert.
        if self._store.is_predictions_seeded():
            return
        legacy = (
            load_all_predictions(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_predictions_once(legacy)

    def append(self, record: dict[str, object]) -> None:
        self._maybe_seed()
        self._store.append_prediction(record)

    def read_all(self) -> list[dict[str, object]]:
        self._maybe_seed()
        return self._store.load_predictions()


class SqliteOwnedBacklogLedger:
    """SQLite-backed :class:`~maestro.stats.OwnedBacklogLedgerBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's snapshot operations, mirroring :class:`SqliteDeedLedger`. On first
    access, if the store has never been seeded and a legacy
    ``owned_backlog_ledger.jsonl`` exists, it seeds the store once from
    :func:`~maestro.stats.load_all_backlog_snapshots` so enabling the flag
    preserves the cycle-over-cycle history — and the first post-flip delta still
    has a prior point to subtract from. The seed fires at most once per DB
    lifetime — a *durable* marker (``is_owned_backlog_seeded``), not a transient
    "store is empty" check, guards it, so a ledger that legitimately drains and
    restarts is never re-seeded from the frozen legacy JSONL. Seed first, then
    mark: a crash between the two leaves the store populated, so the next start's
    "already populated" check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so a drained ledger is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start; ``seed_backlog_snapshots
        # _once`` re-checks the marker while holding the ``BEGIN IMMEDIATE`` write
        # lock, so two processes racing their very first seed over the shared
        # ``state.db`` cannot both insert (which would double the history). Seed
        # first, then mark — both inside that one atomic transaction.
        if self._store.is_owned_backlog_seeded():
            return
        legacy = (
            load_all_backlog_snapshots(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_backlog_snapshots_once(legacy)

    def record(
        self,
        iteration: int,
        count: int,
        *,
        now: str | None = None,
    ) -> BacklogSnapshot:
        self._maybe_seed()
        snapshot = build_backlog_snapshot(iteration, count, now=now)
        self._store.append_backlog_snapshot(snapshot)
        return snapshot

    def recent(self, n: int) -> list[BacklogSnapshot]:
        self._maybe_seed()
        return self._store.recent_backlog_snapshots(n)


class SqlitePrProvenanceLedger:
    """SQLite-backed :class:`~maestro.remediation_tracker.PrProvenanceBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's provenance operations, mirroring :class:`SqliteDeedLedger`. On first
    access, if the store has never been seeded and a legacy
    ``pending_pr_provenance.jsonl`` exists, it seeds the store once from
    :func:`~maestro.remediation_tracker.load_all_provenance_records` so enabling
    the flag preserves the readmission window across the flip. The seed fires at
    most once per DB lifetime — a *durable* marker (``is_pr_provenance_seeded``),
    not a transient "store is empty" check, guards it, so a window that
    legitimately ages out and restarts is never re-seeded from the frozen legacy
    JSONL. Seed first, then mark: a crash between the two leaves the store
    populated, so the next start's "already populated" check skips the re-seed
    before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever, so an aged-out window is never
        # re-seeded from the frozen legacy JSONL. This lock-free read is only a
        # fast path for the common already-seeded start; ``seed_provenance_once``
        # re-checks the marker while holding the ``BEGIN IMMEDIATE`` write lock,
        # so two processes racing their very first seed over the shared
        # ``state.db`` cannot both insert (which would double the window). Seed
        # first, then mark — both inside that one atomic transaction.
        if self._store.is_pr_provenance_seeded():
            return
        legacy = (
            load_all_provenance_records(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_provenance_once(legacy)

    def record(self, pr_number: int, channel: str) -> None:
        self._maybe_seed()
        # Mirror the JSONL writer's guard (``log_pending_pr_provenance``): a
        # non-positive PR number carries no channel signal (a synthetic or
        # unresolved PR), so it is dropped rather than persisted — the sensor
        # must not count phantom admissions.
        if pr_number <= 0:
            return
        self._store.append_provenance(
            ProvenanceRecord(pr_number=pr_number, channel=channel),
        )

    def recent(self, n: int) -> list[ProvenanceRecord]:
        self._maybe_seed()
        return self._store.recent_provenance(n)


class SqliteSettlementReconciliationLedger:
    """SQLite-backed :class:`~maestro.settlement_record_reconciler.SettlementReconciliationBackend`.

    Wraps a :class:`StateStore`, delegating ``append`` to the store's
    settlement-reconciliation operation, mirroring :class:`SqlitePrProvenanceLedger`.
    On first access, if the store has never been seeded and a legacy
    ``settlement_reconciliations.jsonl`` exists, it seeds the store once from
    :func:`~maestro.settlement_record_reconciler.load_all_settlement_reconciliations`
    so enabling the flag preserves the operator-inspectable trace across the flip.
    The seed fires at most once per DB lifetime — a *durable* marker
    (``is_settlement_reconciliations_seeded``), not a transient "store is empty"
    check, guards it, so a trace that legitimately restarts is never re-seeded from
    the frozen legacy JSONL. Seed first, then mark: a crash between the two leaves
    the store populated, so the next start's "already populated" check skips the
    re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_settlement_reconciliations_once`` re-checks the marker while
        # holding the ``BEGIN IMMEDIATE`` write lock, so two processes racing
        # their very first seed over the shared ``state.db`` cannot both insert.
        if self._store.is_settlement_reconciliations_seeded():
            return
        legacy = (
            load_all_settlement_reconciliations(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_settlement_reconciliations_once(legacy)

    def append(self, record: SettlementReconciliationRecord) -> None:
        self._maybe_seed()
        self._store.append_settlement_reconciliation(record)


class SqliteObligationForkLedger:
    """SQLite-backed :class:`~maestro.remediation_tracker.ObligationForkBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's obligation-fork operations, mirroring :class:`SqlitePrProvenanceLedger`.
    On first access, if the store has never been seeded and a legacy
    ``obligation_fork_markers.jsonl`` exists, it seeds the store once from
    :func:`~maestro.remediation_tracker.load_all_obligation_forks` so enabling the
    flag preserves the in-flight fork markers across the flip. The seed fires at
    most once per DB lifetime — a *durable* marker (``is_obligation_fork_seeded``),
    not a transient "store is empty" check, guards it, so markers that
    legitimately age out as their tasks complete are never re-seeded from the
    frozen legacy JSONL. Seed first, then mark: a crash between the two leaves the
    store populated, so the next start's "already populated" check skips the
    re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start; ``seed_obligation_forks
        # _once`` re-checks the marker while holding the ``BEGIN IMMEDIATE`` write
        # lock, so two processes racing their very first seed over the shared
        # ``state.db`` cannot both insert. Seed first, then mark — both inside
        # that one atomic transaction.
        if self._store.is_obligation_fork_seeded():
            return
        legacy = (
            load_all_obligation_forks(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_obligation_forks_once(legacy)

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
        self._maybe_seed()
        # Mirror the JSONL writer's guard (``record_obligation_fork_truth``): an
        # empty ``task_id`` carries no key to look up, so it is dropped rather
        # than persisted — a marker that could never be read must never be
        # stored. The store's ``INSERT OR IGNORE`` then enforces the same
        # per-``(task_id, pr_number)`` first-wins idempotency the JSONL writer's
        # pre-append scan does.
        if not task_id:
            return
        self._store.append_obligation_fork(
            ObligationForkRecord(
                task_id=task_id,
                pr_number=pr_number,
                head_sha=head_sha,
                remote_sha=remote_sha,
                branch=branch,
                remote_is_ancestor=remote_is_ancestor,
            ),
        )

    def get(self, task_id: str) -> ObligationFork | None:
        self._maybe_seed()
        return self._store.get_obligation_fork_for_task(task_id)


class SqliteFetchTruthLedger:
    """SQLite-backed :class:`~maestro.remediation_tracker.FetchTruthBackend`.

    Wraps a :class:`StateStore`, delegating the three backend methods to the
    store's fetch-truth operations, mirroring :class:`SqliteObligationForkLedger`.
    On first access, if the store has never been seeded and a legacy
    ``fetch_truth_markers.jsonl`` exists, it seeds the store once from
    :func:`~maestro.remediation_tracker.load_all_fetch_truth_markers` so enabling
    the flag preserves the in-flight fetch-truth markers across the flip. The seed
    fires at most once per DB lifetime — a *durable* marker
    (``is_fetch_truth_seeded``), not a transient "store is empty" check, guards
    it, so markers that legitimately age out as their tasks complete are never
    re-seeded from the frozen legacy JSONL. Seed first, then mark: a crash between
    the two leaves the store populated, so the next start's "already populated"
    check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start; ``seed_fetch_truth_once``
        # re-checks the marker while holding the ``BEGIN IMMEDIATE`` write lock,
        # so two processes racing their very first seed over the shared
        # ``state.db`` cannot both insert. Seed first, then mark — both inside
        # that one atomic transaction.
        if self._store.is_fetch_truth_seeded():
            return
        legacy = (
            load_all_fetch_truth_markers(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_fetch_truth_once(legacy)

    def record(
        self,
        task_id: str,
        pr_number: int,
        is_ancestor: bool,
        head_sha: str = "",
    ) -> None:
        self._maybe_seed()
        # Mirror the JSONL writer's guard (``record_fetch_truth``): an empty
        # ``task_id`` carries no key to look up, so it is dropped rather than
        # persisted — a marker that could never be read must never be stored.
        if not task_id:
            return
        self._store.append_fetch_truth(
            FetchTruthRecord(
                task_id=task_id,
                pr_number=pr_number,
                is_ancestor=is_ancestor,
                head_sha=head_sha,
            ),
        )

    def has(self, task_id: str) -> bool:
        self._maybe_seed()
        return self._store.has_fetch_truth_for_task(task_id)

    def relap(self, pr_number: int, head_sha: str) -> bool:
        self._maybe_seed()
        return self._store.fetch_truth_relap_exists(pr_number, head_sha)


class SqlitePreflightPassStore:
    """SQLite-backed :class:`~maestro.remediation_tracker.PreflightPassBackend`.

    Wraps a :class:`StateStore`, delegating the two backend methods to the
    store's preflight-pass operations, mirroring :class:`SqliteFetchTruthLedger`.
    On first access, if the store has never been seeded and a legacy
    ``preflight_passes.json`` exists, it seeds the store once from
    :func:`~maestro.remediation_tracker.load_all_preflight_passes` so enabling
    the flag preserves the in-flight passing heads across the flip. The seed
    fires at most once per DB lifetime — a *durable* marker
    (``is_preflight_passes_seeded``), not a transient "store is empty" check,
    guards it, so a PR whose recorded head legitimately advances is never reset
    by the frozen legacy JSON. Seed first, then mark: a crash between the two
    leaves the store populated, so the next start's "already populated" check
    skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_dir = legacy_json_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_preflight_passes_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert. Seed first,
        # then mark — both inside that one atomic transaction.
        if self._store.is_preflight_passes_seeded():
            return
        legacy = (
            load_all_preflight_passes(self._legacy_json_dir)
            if self._legacy_json_dir is not None
            else []
        )
        self._store.seed_preflight_passes_once(legacy)

    def record(self, pr_number: int, head_sha: str) -> None:
        self._maybe_seed()
        self._store.record_preflight_pass(pr_number, head_sha)

    def should_skip(self, pr_number: int, head_sha: str) -> bool:
        self._maybe_seed()
        return self._store.should_skip_preflight(pr_number, head_sha)


class SqliteDroppedDirectiveLedger:
    """SQLite-backed :class:`~maestro.directive_ledger.DroppedDirectiveBackend`.

    Wraps a :class:`StateStore`, delegating ``offer`` / ``replay`` to the store's
    dropped-directive operations, mirroring :class:`SqliteFetchTruthLedger`. On
    first access, if the store has never been seeded and a legacy
    ``dropped_directives.jsonl`` exists, it seeds the store once from
    :func:`~maestro.directive_ledger.load_all_dropped_directives` so enabling the
    flag preserves the in-flight dropped directives across the flip. The seed
    fires at most once per DB lifetime — a *durable* marker
    (``is_dropped_directives_seeded``), not a transient "store is empty" check,
    guards it, so entries that legitimately drain (replayed, pruned, or aged out)
    are never re-seeded from the frozen legacy JSONL. Seed first, then mark: a
    crash between the two leaves the store populated, so the next start's "already
    populated" check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_dropped_directives_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very first
        # seed over the shared ``state.db`` cannot both insert. Seed first, then
        # mark — both inside that one atomic transaction.
        if self._store.is_dropped_directives_seeded():
            return
        legacy = (
            load_all_dropped_directives(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_dropped_directives_once(legacy)

    def offer(self, entry: DroppedDirective) -> None:
        self._maybe_seed()
        self._store.offer_dropped_directive(entry)

    def replay(
        self, is_dead: Callable[[DroppedDirective], bool],
    ) -> str | None:
        self._maybe_seed()
        return self._store.replay_dropped_directive(is_dead)


class SqliteSettledVerdictsLedger:
    """SQLite-backed :class:`~maestro.audit_checks_settlement.SettledVerdictsBackend`.

    Wraps a :class:`StateStore`, delegating the two collapse reads and the append
    to the store's settled-verdicts operations, mirroring
    :class:`SqlitePreflightPassStore`. On first access, if the store has never
    been seeded and a legacy ``settled_verdicts.jsonl`` exists, it seeds the store
    once from :func:`~maestro.audit_checks_settlement.load_all_settled_verdicts` so enabling
    the flag preserves the in-flight settle/retract state across the flip. The
    seed fires at most once per DB lifetime — a *durable* marker
    (``is_settled_verdicts_seeded``), not a transient "store is empty" check,
    guards it, so a PR whose settlement state legitimately advances (a settle,
    then a retraction on reopen) is never reset by the frozen legacy JSONL. Seed
    first, then mark: a crash between the two leaves the store populated, so the
    next start's "already populated" check skips the re-seed before stamping the
    marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_settled_verdicts_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very first
        # seed over the shared ``state.db`` cannot both insert. Seed first, then
        # mark — both inside that one atomic transaction.
        if self._store.is_settled_verdicts_seeded():
            return
        legacy = (
            load_all_settled_verdicts(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_settled_verdicts_once(legacy)

    def effective_settled_prs(self) -> frozenset[int]:
        self._maybe_seed()
        return self._store.effective_settled_prs()

    def merged_settled_prs(self) -> frozenset[int]:
        self._maybe_seed()
        return self._store.merged_settled_prs()

    def append(self, records: Iterable[SettledVerdictRecord]) -> None:
        self._maybe_seed()
        self._store.append_settled_verdicts(list(records))


class SqliteStandDownVerdictLedger:
    """SQLite-backed :class:`~maestro.remediation_tracker.StandDownVerdictBackend`.

    Wraps a :class:`StateStore`, delegating the nine backend methods to the
    store's stand-down operations, mirroring :class:`SqliteFetchTruthLedger` (the
    other append-only, no-unique-key ledger). On first access, if the store has
    never been seeded and a legacy ``stand_down_verdicts.jsonl`` exists, it seeds
    the store once from
    :func:`~maestro.remediation_tracker.load_all_stand_down_records` so enabling
    the flag preserves the in-flight suppression brakes across the flip. The seed
    fires at most once per DB lifetime — a *durable* marker
    (``is_stand_down_verdicts_seeded``), not a transient "store is empty" check,
    guards it, so verdicts that legitimately release (a settled PR, a new head, a
    human demand) are never re-seeded from the frozen legacy JSONL. Seed first,
    then mark: a crash between the two leaves the store populated, so the next
    start's "already populated" check skips the re-seed before stamping the
    marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_stand_down_verdicts_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very first
        # seed over the shared ``state.db`` cannot both insert. Seed first, then
        # mark — both inside that one atomic transaction.
        if self._store.is_stand_down_verdicts_seeded():
            return
        legacy = (
            load_all_stand_down_records(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else []
        )
        self._store.seed_stand_down_verdicts_once(legacy)

    def append_verdict(self, row: StandDownRow) -> None:
        self._maybe_seed()
        self._store.append_stand_down_verdict(row)

    def append_release(self, pr: int, released_at: str) -> None:
        self._maybe_seed()
        self._store.append_stand_down_release(pr, released_at)

    def find(self, pr: int, head_sha: str) -> StandDownVerdict | None:
        self._maybe_seed()
        return self._store.stand_down_find(pr, head_sha)

    def has_live(self, pr: int) -> bool:
        self._maybe_seed()
        return self._store.stand_down_has_live(pr)

    def verdict_rows(self) -> list[StandDownRow]:
        self._maybe_seed()
        return self._store.stand_down_verdict_rows()

    def written_count(self) -> int:
        self._maybe_seed()
        return self._store.stand_down_written_count()

    def recorded_timestamps(self) -> list[str]:
        self._maybe_seed()
        return self._store.stand_down_recorded_timestamps()

    def banked_pr_ids(self) -> set[int]:
        self._maybe_seed()
        return self._store.stand_down_banked_pr_ids()

    def verdict_count_for_pr(self, pr: int) -> int:
        self._maybe_seed()
        return self._store.stand_down_verdict_count_for_pr(pr)


class SqliteUnconfirmedOwnLandingStore:
    """SQLite-backed :class:`~maestro.audit_checks_phantom.UnconfirmedOwnLandingBackend`.

    Wraps a :class:`StateStore`, delegating the single ``prune_and_touch``
    read-modify-write to the store's one-transaction
    :meth:`StateStore.prune_and_touch_unconfirmed_landing`, mirroring how
    :class:`SqliteDroppedDirectiveLedger` (the other bounded RMW ledger) delegates
    to :meth:`StateStore.replay_dropped_directive`. On first
    access, if the store has never been seeded and a legacy
    ``unconfirmed_landing_first_seen.json`` exists, it seeds the store once from
    :func:`~maestro.audit_checks_phantom.load_all_unconfirmed_landing_seen` so enabling the
    flag preserves the in-flight first-seen ages across the flip. The seed fires at
    most once per DB lifetime — a *durable* marker
    (``is_unconfirmed_landing_seeded``), not a transient "store is empty" check,
    guards it, so a SHA whose first-seen legitimately drops out (it landed or
    settled, so the next prune omits it) is never re-seeded from the frozen legacy
    JSON. Seed first, then mark: a crash between the two leaves the store
    populated, so the next start's "already populated" check skips the re-seed
    before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_jsonl_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_jsonl_dir = legacy_jsonl_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_unconfirmed_landing_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very first
        # seed over the shared ``state.db`` cannot both insert. Seed first, then
        # mark — both inside that one atomic transaction.
        if self._store.is_unconfirmed_landing_seeded():
            return
        legacy = (
            load_all_unconfirmed_landing_seen(self._legacy_jsonl_dir)
            if self._legacy_jsonl_dir is not None
            else {}
        )
        self._store.seed_unconfirmed_landing_once(legacy)

    def prune_and_touch(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        self._maybe_seed()
        # The store owns the whole load-prune-write inside one transaction
        # (mirroring SqliteDroppedDirectiveLedger.replay → replay_dropped_directive),
        # so the read and the atomic swap never straddle a concurrent writer.
        return self._store.prune_and_touch_unconfirmed_landing(observed, iteration)


class SqliteOrphanedCureStore:
    """SQLite-backed :class:`~maestro.audit_checks_phantom.OrphanedCureBackend`.

    Wraps a :class:`StateStore`, delegating the atomic ``prune_and_touch``
    read-modify-write to the store's one-transaction
    :meth:`StateStore.prune_and_touch_orphaned_cure` (mirroring how
    :class:`SqliteUnconfirmedOwnLandingStore` delegates) and the read-only
    ``load`` — the second consumer this map has, unlike its single-consumer
    sibling — to :meth:`StateStore.load_orphaned_cure_seen`. On first access, if
    the store has never been seeded and a legacy ``orphaned_cure_first_seen.json``
    exists, it seeds the store once from
    :func:`~maestro.audit_checks_phantom.load_all_orphaned_cure_seen` so enabling the flag
    preserves the in-flight first-seen ages across the flip; the ``legacy_json_dir``
    kwarg matches the single-JSON-file :class:`SqlitePreflightPassStore` precedent
    (the sidecar is a plain JSON file, not JSONL). The seed fires at most once per
    DB lifetime — a *durable* marker (``is_orphaned_cure_seeded``), not a transient
    "store is empty" check, guards it, so a plan id whose first-seen legitimately
    drops out (it left ``implemented``, so the next prune omits it) is never
    re-seeded from the frozen legacy JSON. Seed first, then mark: a crash between
    the two leaves the store populated, so the next start's "already populated"
    check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_dir = legacy_json_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start; ``seed_orphaned_cure_once``
        # re-checks the marker while holding the ``BEGIN IMMEDIATE`` write lock, so
        # two processes racing their very first seed over the shared ``state.db``
        # cannot both insert. Seed first, then mark — both inside that one atomic
        # transaction.
        if self._store.is_orphaned_cure_seeded():
            return
        legacy = (
            load_all_orphaned_cure_seen(self._legacy_json_dir)
            if self._legacy_json_dir is not None
            else {}
        )
        self._store.seed_orphaned_cure_once(legacy)

    def prune_and_touch(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        self._maybe_seed()
        # The store owns the whole load-prune-write inside one transaction, so the
        # read and the atomic swap never straddle a concurrent writer.
        return self._store.prune_and_touch_orphaned_cure(observed, iteration)

    def load(self) -> dict[str, int]:
        self._maybe_seed()
        return self._store.load_orphaned_cure_seen()


class SqliteImpactReviewSurfacedStore:
    """SQLite-backed :class:`~maestro.audit_checks.ImpactReviewSurfacedBackend`.

    Wraps a :class:`StateStore`, delegating the append-only ``load`` /
    ``record`` set surface to the store's
    :meth:`StateStore.load_impact_review_surfaced` /
    :meth:`StateStore.record_impact_review_surfaced` — the two-method seam the
    single-consumer, never-pruned surfaced set uses, unlike the read-modify-write
    map siblings. On first access, if the store has never been seeded and a legacy
    ``impact_review_surfaced.json`` exists, it seeds the store once from
    :func:`~maestro.audit_checks.load_all_impact_review_surfaced` so enabling the
    flag preserves the already-surfaced ids across the flip; the ``legacy_json_dir``
    kwarg matches the single-JSON-file :class:`SqliteOrphanedCureStore` precedent
    (the sidecar is a plain JSON list, not JSONL). The seed fires at most once per
    DB lifetime — a *durable* marker (``is_impact_review_surfaced_seeded``), not a
    transient "store is empty" check, guards it, so the append-only set is never
    re-seeded from the frozen legacy JSON. Seed first, then mark: a crash between
    the two leaves the store populated, so the next start's "already populated"
    check skips the re-seed before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_dir = legacy_json_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_impact_review_surfaced_once`` re-checks the marker while holding
        # the ``BEGIN IMMEDIATE`` write lock, so two processes racing their very
        # first seed over the shared ``state.db`` cannot both insert. Seed first,
        # then mark — both inside that one atomic transaction.
        if self._store.is_impact_review_surfaced_seeded():
            return
        legacy = (
            load_all_impact_review_surfaced(self._legacy_json_dir)
            if self._legacy_json_dir is not None
            else set()
        )
        self._store.seed_impact_review_surfaced_once(legacy)

    def load(self) -> set[str]:
        self._maybe_seed()
        return self._store.load_impact_review_surfaced()

    def record(self, version_ids: list[str]) -> None:
        self._maybe_seed()
        self._store.record_impact_review_surfaced(version_ids)


class SqliteMergeRequestServicedStore:
    """SQLite-backed :class:`~maestro.outcome_grounding.MergeRequestServicedBackend`.

    Wraps a :class:`StateStore`, delegating the two-method :meth:`is_serviced` /
    :meth:`record_serviced` surface to the store's
    :meth:`StateStore.is_merge_request_serviced` /
    :meth:`StateStore.record_merge_request_serviced` — the append-only,
    single-consumer keyed ``(pr, sha)`` set the mergeable-flip actuator uses. On
    first access, if the store has never been seeded and a legacy
    ``mergeable_merge_requests.json`` exists, it seeds the store once from
    :func:`~maestro.outcome_grounding.load_all_serviced_merge_requests` so enabling
    the flag preserves the already-serviced heads across the flip; the
    ``legacy_json_dir`` kwarg matches the single-JSON-file
    :class:`SqliteImpactReviewSurfacedStore` precedent (the sidecar is a plain JSON
    object, not JSONL). The seed fires at most once per DB lifetime — a *durable*
    marker (``is_mergeable_serviced_seeded``), not a transient "store is empty"
    check, guards it, so the append-only set is never re-seeded from the frozen
    legacy JSON. Seed first, then mark: a crash between the two leaves the store
    populated, so the next start's "already populated" check skips the re-seed
    before stamping the marker.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        legacy_json_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._legacy_json_dir = legacy_json_dir
        self._seed_checked = False

    def _maybe_seed(self) -> None:
        if self._seed_checked:
            return
        self._seed_checked = True
        # Durable, per-DB-lifetime guard (see the class docstring): a persisted
        # marker fires the seed at most once ever. This lock-free read is only a
        # fast path for the common already-seeded start;
        # ``seed_mergeable_serviced_once`` re-checks the marker while holding the
        # ``BEGIN IMMEDIATE`` write lock, so two processes racing their very first
        # seed over the shared ``state.db`` cannot both insert. Seed first, then
        # mark — both inside that one atomic transaction.
        if self._store.is_mergeable_serviced_seeded():
            return
        legacy = (
            load_all_serviced_merge_requests(self._legacy_json_dir)
            if self._legacy_json_dir is not None
            else {}
        )
        self._store.seed_mergeable_serviced_once(legacy)

    def is_serviced(self, pr_number: int, head_sha: str) -> bool:
        self._maybe_seed()
        return self._store.is_merge_request_serviced(pr_number, head_sha)

    def record_serviced(self, pr_number: int, head_sha: str) -> None:
        self._maybe_seed()
        self._store.record_merge_request_serviced(pr_number, head_sha)
