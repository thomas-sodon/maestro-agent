"""Tests for the SQLite state store and its task-queue adapter (proposal 106).

Every SQLite test uses ``sqlite3.connect(":memory:")`` (directly or injected)
so no real database file is opened and the ``mem_fs`` fixture is never handed to
``sqlite3.connect`` — the seed tests use ``mem_fs`` only for the legacy JSON that
the JSON backend reads.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from maestro import state_store
from maestro.audit_checks import (
    GATE_ACTIVATIONS_FILENAME,
    AuditFinding,
    GateActivation,
    JsonImpactReviewSurfacedStore,
    JsonlGateActivationLedger,
    load_all_impact_review_surfaced,
)
from maestro.audit_checks_calibration import (
    PREDICTIONS_FILENAME,
    PREMISE_OBSERVATIONS_FILENAME,
    JsonlPredictionLedger,
    JsonlPremiseLedger,
    PremiseObservation,
)
from maestro.audit_checks_phantom import (
    JsonOrphanedCureStore,
    JsonUnconfirmedOwnLandingStore,
    load_all_orphaned_cure_seen,
    load_all_unconfirmed_landing_seen,
)
from maestro.audit_checks_settlement import (
    JsonlSettledVerdictsLedger,
    SettledVerdictRecord,
    load_all_settled_verdicts,
)
from maestro.conscious_workspace import (
    CONSCIOUS_EPISODES_FILENAME,
    MAX_MOMENT_CHARS,
    MAX_MOMENT_ITEMS,
    AttentionSchema,
    BroadcastReceipt,
    ConsciousEpisode,
    ConsciousEpisodeBackend,
    ConsciousMoment,
    JsonlConsciousEpisodeLedger,
    build_workspace_item,
    episode_to_dict,
)
from maestro.cost_tracking import CostSample, _as_utc
from maestro.deed_ledger import (
    DEED_LEDGER_FILENAME,
    Deed,
    DeedType,
    record_deed,
)
from maestro.directive_ledger import (
    _MAX_ENTRIES,
    DroppedDirective,
    JsonlDroppedDirectiveLedger,
)
from maestro.escalation_bridge import (
    EscalationLedgerBackend,
    JsonEscalationLedger,
    StanddownRecord,
    deliver_handoff,
    has_standdown,
    prune_expired_standdowns,
    prune_resolved_standdowns,
    unserviced_comment_ids,
)
from maestro.escalation_bridge import (
    UnlandedCureHandoff as _Handoff,
)
from maestro.lever_capture import (
    LEVER_ATTEMPTS_FILENAME,
    JsonlLeverAttemptsLedger,
    LeverAttempt,
    build_lever_attempt,
)
from maestro.memory import (
    AUDIT_HISTORY_FILENAME,
    QUESTION_RESOLUTIONS_FILENAME,
    REJECTION_HISTORY_FILENAME,
    VERSION_ARCHIVE_FILENAME,
    AuditResult,
    JsonlAuditHistoryLedger,
    JsonlQuestionResolutionLedger,
    JsonlRejectionHistoryLedger,
    JsonlVersionArchiveLedger,
    QuestionResolution,
    RejectionRecord,
    VersionRecord,
)
from maestro.outcome_grounding import (
    JsonMergeRequestServicedStore,
    load_all_serviced_merge_requests,
)
from maestro.pr_ledger import (
    JsonPRLedger,
    PRLedger,
    PRLedgerEntry,
)
from maestro.remediation_tracker import (
    FetchTruthRecord,
    JsonlStandDownVerdictLedger,
    JsonRemediationTrackerStore,
    ObligationFork,
    ObligationForkRecord,
    PreflightPassRecord,
    ProvenanceRecord,
    RemediationTrackerBackend,
    StandDownRow,
    StandDownVerdict,
    TrackerState,
    find_stand_down,
    has_live_stand_down,
    load_all_stand_down_records,
    log_pending_pr_provenance,
    read_chosen_stand_down_acts,
    record_fetch_truth,
    record_obligation_fork_truth,
    record_preflight_pass,
    record_stand_down,
    release_stand_downs,
)
from maestro.search import bm25_rank
from maestro.settlement_record_reconciler import (
    JsonlSettlementReconciliationLedger,
    SettlementReconciliationRecord,
    load_all_settlement_reconciliations,
)
from maestro.standdown_validity import StanddownClaim
from maestro.state_store import (
    _BUSY_TIMEOUT_MS,
    SqliteAuditHistoryLedger,
    SqliteConsciousEpisodeLedger,
    SqliteCostSampleStore,
    SqliteDeedLedger,
    SqliteDroppedDirectiveLedger,
    SqliteEscalationLedger,
    SqliteFetchTruthLedger,
    SqliteGateActivationLedger,
    SqliteImpactReviewSurfacedStore,
    SqliteJournalSearchIndex,
    SqliteLeverAttemptsLedger,
    SqliteMergeRequestServicedStore,
    SqliteObligationForkLedger,
    SqliteOrphanedCureStore,
    SqliteOwnedBacklogLedger,
    SqlitePredictionLedger,
    SqlitePreflightPassStore,
    SqlitePremiseLedger,
    SqlitePRLedger,
    SqlitePrProvenanceLedger,
    SqliteQuestionResolutionLedger,
    SqliteRejectionHistoryLedger,
    SqliteRemediationTrackerStore,
    SqliteSettledVerdictsLedger,
    SqliteSettlementReconciliationLedger,
    SqliteStandDownVerdictLedger,
    SqliteTaskQueue,
    SqliteUnconfirmedOwnLandingStore,
    SqliteVersionArchiveLedger,
    StateStore,
    _cost_sample_to_row,
)
from maestro.stats import (
    _OWNED_BACKLOG_LEDGER_FILE,
    BacklogSnapshot,
    record_owned_backlog_snapshot,
)
from maestro.task_queue import QueuedTask, TaskQueue


def _record_select_in_transaction(store: StateStore) -> list[bool]:
    """Record ``in_transaction`` at each ``SELECT`` *store* issues.

    A read-modify-write that takes the write lock up front (``BEGIN
    IMMEDIATE``) is already in a transaction when it issues its leading read;
    one that relies on bare ``with self._conn:`` is not, because Python's
    legacy deferred isolation only begins the transaction at the first DML.
    The observation rides sqlite3's trace callback, since the C connection's
    ``execute`` attribute is read-only and cannot be wrapped.
    """
    observed: list[bool] = []

    def _trace(sql: str) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            observed.append(store._conn.in_transaction)

    store._conn.set_trace_callback(_trace)
    return observed


def _cost(
    *,
    ts: str = "2026-01-01T00:00:00+00:00",
    cost: float = 0.10,
    caller: str = "introspect",
    estimated: bool = False,
    unmetered: bool = False,
    provider: str | None = None,
) -> CostSample:
    """Build a fully-specified :class:`CostSample` for deterministic equality."""
    return CostSample(
        timestamp=ts,
        cost_usd=cost,
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        cache_creation_tokens=100,
        caller=caller,
        duration_ms=1500,
        estimated=estimated,
        unmetered=unmetered,
        provider=provider,
    )


def _task(
    task_id: str,
    goal: str,
    *,
    linked_question: str | None = None,
    rationale: str | None = None,
    acceptance_criteria: list[str] | None = None,
    workspace_moment_id: str | None = None,
    workspace_item_ids: list[str] | None = None,
) -> QueuedTask:
    """Build a fully-specified :class:`QueuedTask` for deterministic equality."""
    return QueuedTask(
        task_id=task_id,
        goal=goal,
        linked_question=linked_question,
        rationale=rationale,
        created_at="2026-01-01T00:00:00+00:00",
        last_touched_at="2026-01-02T00:00:00+00:00",
        acceptance_criteria=acceptance_criteria,
        workspace_moment_id=workspace_moment_id,
        workspace_item_ids=workspace_item_ids,
    )


# ---------------------------------------------------------------------------
# StateStore construction / schema
# ---------------------------------------------------------------------------


def test_state_store_requires_path_or_connection() -> None:
    with pytest.raises(ValueError, match="requires either a db_path"):
        StateStore()


def test_state_store_creates_task_queue_table_from_memory_path() -> None:
    store = StateStore(":memory:")
    # An empty, freshly-created store round-trips as an empty list.
    assert store.load_task_queue() == []


def test_state_store_accepts_injected_connection() -> None:
    conn = sqlite3.connect(":memory:")
    StateStore(connection=conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='task_queue'",
    ).fetchall()
    assert len(rows) == 1


def test_state_store_installs_busy_timeout_on_injected_connection() -> None:
    """__init__ normalizes a zero-timeout connection to a non-zero busy wait.

    ``sqlite3.connect`` already defaults the busy timeout to 5000ms for the
    path / ``":memory:"`` branches, but an *injected* connection may carry a
    shorter (or zero) timeout. ``StateStore`` sets it explicitly so lock
    contention between the several connections that share ``state.db`` when the
    flag is on waits and retries rather than failing fast with "database is
    locked" (proposal 106 review finding).
    """
    conn = sqlite3.connect(":memory:", timeout=0)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    StateStore(connection=conn)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == _BUSY_TIMEOUT_MS


# ---------------------------------------------------------------------------
# StateStore task-queue methods
# ---------------------------------------------------------------------------


def test_replace_and_load_round_trip() -> None:
    store = StateStore(":memory:")
    tasks = [
        _task("t1", "first", linked_question="q1", rationale="r1"),
        _task("t2", "second", acceptance_criteria=["c1", "c2"]),
    ]
    store.replace_task_queue(tasks)
    assert store.load_task_queue() == tasks


def test_load_preserves_fifo_order() -> None:
    store = StateStore(":memory:")
    tasks = [_task(f"t{i}", f"goal {i}") for i in range(5)]
    store.replace_task_queue(tasks)
    loaded = store.load_task_queue()
    assert [t.task_id for t in loaded] == ["t0", "t1", "t2", "t3", "t4"]


def test_replace_overwrites_previous_contents() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue([_task("old", "old goal")])
    store.replace_task_queue([_task("new", "new goal")])
    loaded = store.load_task_queue()
    assert [t.task_id for t in loaded] == ["new"]


def test_replace_with_empty_list_clears_store() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue([_task("t1", "goal")])
    store.replace_task_queue([])
    assert store.load_task_queue() == []


def test_pop_head_returns_and_removes_lowest_position() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue([_task("t1", "first"), _task("t2", "second")])
    head = store.pop_task_queue_head()
    assert head is not None
    assert head.task_id == "t1"
    assert [t.task_id for t in store.load_task_queue()] == ["t2"]


def test_pop_head_on_empty_store_returns_none() -> None:
    store = StateStore(":memory:")
    assert store.pop_task_queue_head() is None


def test_pop_task_queue_head_reads_inside_write_transaction() -> None:
    """The SELECT of the head row runs inside the same write transaction as the
    DELETE, so two ``state.db`` connections cannot both fetch — and both
    dispatch — the same queued task."""
    store = StateStore(":memory:")
    store.replace_task_queue([_task("t1", "first"), _task("t2", "second")])
    observed = _record_select_in_transaction(store)

    head = store.pop_task_queue_head()

    assert head is not None
    assert head.task_id == "t1"
    assert observed[0] is True
    assert store._conn.in_transaction is False


def test_pop_task_queue_head_empty_still_commits_its_transaction() -> None:
    """The early ``return None`` returns from inside the open write
    transaction, which the context manager still commits — no leaked lock."""
    store = StateStore(":memory:")
    observed = _record_select_in_transaction(store)

    assert store.pop_task_queue_head() is None
    assert observed[0] is True
    assert store._conn.in_transaction is False


def test_acceptance_criteria_none_round_trips() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue([_task("t1", "goal", acceptance_criteria=None)])
    loaded = store.load_task_queue()
    assert loaded[0].acceptance_criteria is None


def test_acceptance_criteria_list_round_trips() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue(
        [_task("t1", "goal", acceptance_criteria=["a", "b"])],
    )
    loaded = store.load_task_queue()
    assert loaded[0].acceptance_criteria == ["a", "b"]


def test_workspace_citation_round_trips_set_and_unset() -> None:
    """The conscious-access citation (proposal 130) round-trips through SQLite
    both when set and when unset, with full ``QueuedTask`` equality."""
    store = StateStore(":memory:")
    tasks = [
        _task(
            "cited", "goal",
            workspace_moment_id="cm-1",
            workspace_item_ids=["wi-1", "wi-2"],
        ),
        _task("uncited", "goal"),
    ]
    store.replace_task_queue(tasks)
    assert store.load_task_queue() == tasks


def test_workspace_item_ids_none_round_trips() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue([_task("t1", "goal", workspace_item_ids=None)])
    loaded = store.load_task_queue()
    assert loaded[0].workspace_item_ids is None


def test_workspace_item_ids_empty_list_round_trips_distinct_from_none() -> None:
    """``[]`` (saw the moment, cited no item) survives as a JSON ``"[]"`` column
    value, staying distinct from ``None`` (the NULL column / never-cited case)."""
    store = StateStore(":memory:")
    store.replace_task_queue(
        [_task("t1", "goal", workspace_moment_id="cm-1", workspace_item_ids=[])],
    )
    loaded = store.load_task_queue()
    assert loaded[0].workspace_item_ids == []
    assert loaded[0].workspace_moment_id == "cm-1"


def test_workspace_citation_survives_pop_head() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue(
        [_task("t1", "g", workspace_moment_id="cm-9", workspace_item_ids=["wi-3"])],
    )
    head = store.pop_task_queue_head()
    assert head is not None
    assert head.workspace_moment_id == "cm-9"
    assert head.workspace_item_ids == ["wi-3"]


def test_opening_store_over_pre_citation_table_adds_columns() -> None:
    """A ``task_queue`` table that predates the citation columns (proposal 130)
    gains them idempotently when a ``StateStore`` opens over the connection —
    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so the
    ``PRAGMA table_info``-guarded ``ALTER TABLE`` back-fill in ``__init__`` is
    what makes the old shape usable, and re-opening is a no-op."""
    conn = sqlite3.connect(":memory:")
    # Simulate the pre-proposal-130 shape: the DDL without the two columns.
    conn.execute(
        "CREATE TABLE task_queue ("
        "position INTEGER PRIMARY KEY, task_id TEXT NOT NULL, goal TEXT NOT NULL, "
        "linked_question TEXT, rationale TEXT, created_at TEXT NOT NULL, "
        "last_touched_at TEXT NOT NULL, acceptance_criteria TEXT)",
    )
    conn.commit()
    cols_before = {
        row[1] for row in conn.execute("PRAGMA table_info(task_queue)")
    }
    assert "workspace_moment_id" not in cols_before
    assert "workspace_item_ids" not in cols_before

    store = StateStore(connection=conn)
    cols_after = {
        row[1] for row in conn.execute("PRAGMA table_info(task_queue)")
    }
    assert "workspace_moment_id" in cols_after
    assert "workspace_item_ids" in cols_after

    # The upgraded table is fully usable: a citation round-trips.
    store.replace_task_queue(
        [_task("t1", "g", workspace_moment_id="cm-1", workspace_item_ids=["wi-1"])],
    )
    loaded = store.load_task_queue()
    assert loaded[0].workspace_moment_id == "cm-1"
    assert loaded[0].workspace_item_ids == ["wi-1"]

    # Re-opening over the already-migrated table is a no-op (no duplicate-column
    # error) and preserves the data.
    reopened = StateStore(connection=conn)
    assert reopened.load_task_queue()[0].workspace_item_ids == ["wi-1"]


def test_task_queue_columns_match_queued_task_fields() -> None:
    """``_TASK_QUEUE_COLUMNS`` mirrors ``QueuedTask``'s fields exactly.

    The task queue is the one ``state.db`` table mapping a dataclass to
    per-field typed columns, so a field added to :class:`QueuedTask` without a
    matching column is a schema drift that only surfaces as a runtime
    ``sqlite3.OperationalError``. This binds the declaration to the dataclass so
    the drift is red here instead."""
    declared_fields = tuple(
        field.name for field in dataclasses.fields(QueuedTask)
    )
    columns = state_store._TASK_QUEUE_COLUMNS
    unmapped = sorted(set(declared_fields) - set(columns))
    orphaned = sorted(set(columns) - set(declared_fields))
    assert unmapped == [], f"QueuedTask fields without a column: {unmapped}"
    assert orphaned == [], f"columns without a QueuedTask field: {orphaned}"
    assert declared_fields == columns, "field and column order diverged"


def test_task_queue_backfill_covers_every_declared_column() -> None:
    """The back-fill is derived from ``_TASK_QUEUE_COLUMNS``, not a literal pair.

    A legacy table missing a column *outside* the proposal-130 pair the old
    literal loop named gains every declared column when a store opens over it,
    so the next added field is repaired by the same loop rather than needing a
    hand-edit here."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE task_queue ("
        "position INTEGER PRIMARY KEY, task_id TEXT NOT NULL, goal TEXT NOT NULL, "
        "linked_question TEXT, rationale TEXT, created_at TEXT NOT NULL, "
        "last_touched_at TEXT NOT NULL)",
    )
    conn.commit()
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(task_queue)")}
    assert "acceptance_criteria" not in cols_before

    store = StateStore(connection=conn)

    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(task_queue)")}
    for column in state_store._TASK_QUEUE_COLUMNS:
        assert column in cols_after

    # The back-filled table is usable end to end.
    store.replace_task_queue(
        [
            _task(
                "t1",
                "g",
                acceptance_criteria=["c1"],
                workspace_moment_id="cm-1",
                workspace_item_ids=["wi-1"],
            ),
        ],
    )
    loaded = store.load_task_queue()
    assert loaded[0].task_id == "t1"
    assert loaded[0].acceptance_criteria == ["c1"]
    assert loaded[0].workspace_moment_id == "cm-1"
    assert loaded[0].workspace_item_ids == ["wi-1"]


def test_task_queue_ddl_matches_declared_columns() -> None:
    """``_TASK_QUEUE_DDL`` declares exactly ``position`` + the column tuple.

    The DDL stays a hand-written literal because each column carries a type and
    a nullability, so this binds it to :data:`_TASK_QUEUE_COLUMNS` — and its
    ``NOT NULL`` set to :data:`_TASK_QUEUE_REQUIRED_COLUMNS` — so a column added
    to one and not the other is red here rather than at runtime."""
    conn = sqlite3.connect(":memory:")
    conn.execute(state_store._TASK_QUEUE_DDL)
    info = list(conn.execute("PRAGMA table_info(task_queue)"))

    ddl_columns = tuple(row[1] for row in info)
    assert ddl_columns == ("position", *state_store._TASK_QUEUE_COLUMNS)

    not_null = {
        row[1] for row in info if row[3] == 1 and row[1] != "position"
    }
    assert not_null == set(state_store._TASK_QUEUE_REQUIRED_COLUMNS)


def test_task_queue_missing_not_null_column_fails_at_construction() -> None:
    """A missing ``NOT NULL`` column raises at construction, not mid-loop.

    SQLite cannot ``ADD COLUMN ... NOT NULL`` without a default, and adding one
    as nullable would feed ``None`` into a ``str``-typed ``QueuedTask`` field,
    so the back-fill skips it and the post-back-fill guard fails loudly at the
    ``StateStore(...)`` call site instead of deferring a bare
    ``sqlite3.OperationalError`` to the first queue read."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE task_queue ("
        "position INTEGER PRIMARY KEY, task_id TEXT NOT NULL, goal TEXT NOT NULL)",
    )
    conn.commit()

    with pytest.raises(
        state_store.StateStoreSchemaError,
        match="task_queue table is missing declared column",
    ) as excinfo:
        StateStore(connection=conn)

    # Every unrepaired column is named, so the operator sees the whole gap; the
    # nullable ones were added in place and so are absent from the message.
    message = str(excinfo.value)
    assert "created_at" in message
    assert "last_touched_at" in message
    assert "linked_question" not in message


# ---------------------------------------------------------------------------
# SqliteTaskQueue adapter — delegation
# ---------------------------------------------------------------------------


def test_adapter_delegates_save_load_head_pop() -> None:
    queue = SqliteTaskQueue(StateStore(":memory:"))
    tasks = [_task("t1", "first"), _task("t2", "second")]
    queue.save(tasks)
    assert queue.load() == tasks
    head = queue.head()
    assert head is not None
    assert head.task_id == "t1"
    popped = queue.pop_head()
    assert popped is not None
    assert popped.task_id == "t1"
    assert [t.task_id for t in queue.load()] == ["t2"]


def test_mutate_task_queue_rebases_onto_the_live_queue() -> None:
    """Curation's delta applies to the post-pop queue, not a stale snapshot.

    The SQLite twin of
    ``test_curation_mutate_does_not_resurrect_concurrently_popped_head``
    (#3561): a head popped inside curation's load → save window must stay
    popped, and a concurrently-enqueued task must survive.
    """
    store = StateStore(":memory:")
    store.replace_task_queue([
        _task("t1", "first"), _task("t2", "second"), _task("t3", "third"),
    ])
    snapshot = store.load_task_queue()
    # The consumer pops the head while the priced curation call is in flight.
    popped = store.pop_task_queue_head()
    assert popped is not None
    assert popped.task_id == "t1"

    snapshot_ids = {q.task_id for q in snapshot}
    survivors = {q.task_id: q for q in snapshot if q.task_id != "t3"}
    fresh = _task("new", "from curation")

    def _rebase(live: list[QueuedTask]) -> int:
        rebased = [
            survivors.get(entry.task_id, entry)
            for entry in live
            if entry.task_id in survivors or entry.task_id not in snapshot_ids
        ]
        live[:] = [*rebased, fresh]
        return len(live)

    assert store.mutate_task_queue(_rebase) == 2
    assert [q.task_id for q in store.load_task_queue()] == ["t2", "new"]


def test_mutate_task_queue_rolls_back_when_the_callback_raises() -> None:
    store = StateStore(":memory:")
    store.replace_task_queue([_task("t1", "first")])

    def _boom(live: list[QueuedTask]) -> None:
        live.clear()
        raise RuntimeError("curation exploded")

    with pytest.raises(RuntimeError, match="curation exploded"):
        store.mutate_task_queue(_boom)
    assert [q.task_id for q in store.load_task_queue()] == ["t1"]


def test_adapter_mutate_serialises_and_cleans_on_write() -> None:
    """The adapter's ``mutate`` rebases live *and* applies clean-on-write."""
    store = StateStore(":memory:")
    queue = SqliteTaskQueue(store)
    queue.save([_task("t1", "first"), _task("t2", "second")])
    popped = queue.pop_head()
    assert popped is not None
    assert popped.task_id == "t1"

    def _curate(live: list[QueuedTask]) -> None:
        # A blank-goal row the JSON backend would drop on read; the SQLite
        # backend must drop it on write, mutate path included.
        live.append(_task("dirty", "   "))

    queue.mutate(_curate)
    assert [q.task_id for q in queue.load()] == ["t2"]


def test_adapter_head_empty_returns_none() -> None:
    queue = SqliteTaskQueue(StateStore(":memory:"))
    assert queue.head() is None


def test_adapter_pop_head_empty_returns_none() -> None:
    queue = SqliteTaskQueue(StateStore(":memory:"))
    assert queue.pop_head() is None


# ---------------------------------------------------------------------------
# SqliteTaskQueue adapter — one-time seed from legacy JSON
# ---------------------------------------------------------------------------


def test_adapter_seeds_from_legacy_json(mem_fs: dict[str, str]) -> None:
    legacy = Path("/fake/data/task_queue.json")
    TaskQueue(legacy).save([_task("t1", "first"), _task("t2", "second")])
    queue = SqliteTaskQueue(StateStore(":memory:"), legacy_json_path=legacy)
    loaded = queue.load()
    assert [t.goal for t in loaded] == ["first", "second"]


def test_adapter_seed_is_one_time_idempotent(mem_fs: dict[str, str]) -> None:
    legacy = Path("/fake/data/task_queue.json")
    TaskQueue(legacy).save([_task("t1", "first")])
    store = StateStore(":memory:")
    first = SqliteTaskQueue(store, legacy_json_path=legacy)
    assert len(first.load()) == 1
    # A second adapter over the already-populated store must not re-seed.
    second = SqliteTaskQueue(store, legacy_json_path=legacy)
    assert len(second.load()) == 1


def test_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    legacy = Path("/fake/data/task_queue.json")
    TaskQueue(legacy).save([_task("t1", "first")])
    queue = SqliteTaskQueue(StateStore(":memory:"), legacy_json_path=legacy)
    queue.load()  # seeds and flips the guard
    queue.save([])  # empty the store
    # Second access must not re-seed despite the store now being empty.
    assert queue.load() == []


def test_adapter_does_not_reseed_on_restart_after_drain(
    mem_fs: dict[str, str],
) -> None:
    """A fresh adapter over a drained-but-persistent store must not re-seed.

    Regression for the durable seed marker: the in-memory ``_seed_checked``
    guard does not survive a restart, and the legacy ``task_queue.json`` is
    never deleted, so a "store is empty" heuristic would resurrect the
    already-consumed task. The store persists across both adapters (same
    ``StateStore`` / connection), standing in for a process restart.
    """
    legacy = Path("/fake/data/task_queue.json")
    TaskQueue(legacy).save([_task("t1", "first")])
    store = StateStore(":memory:")
    # First "process": seed from legacy, then consume the whole queue.
    first = SqliteTaskQueue(store, legacy_json_path=legacy)
    assert len(first.load()) == 1
    assert first.pop_head() is not None
    assert first.load() == []
    # Second "process" over the SAME persistent store: a fresh adapter whose
    # in-memory guard is reset must NOT re-seed from the still-present legacy
    # JSON, matching the drained JSON backend (which returns []).
    second = SqliteTaskQueue(store, legacy_json_path=legacy)
    assert second.load() == []
    assert store.is_task_queue_seeded() is True


def test_adapter_no_seed_without_legacy_path() -> None:
    queue = SqliteTaskQueue(StateStore(":memory:"), legacy_json_path=None)
    assert queue.load() == []


def test_adapter_no_seed_when_legacy_json_absent() -> None:
    queue = SqliteTaskQueue(
        StateStore(":memory:"),
        legacy_json_path=Path("/fake/data/missing.json"),
    )
    assert queue.load() == []


def test_adapter_no_seed_when_legacy_json_empty(
    mem_fs: dict[str, str],
) -> None:
    legacy = Path("/fake/data/task_queue.json")
    mem_fs[str(legacy)] = "[]"
    queue = SqliteTaskQueue(StateStore(":memory:"), legacy_json_path=legacy)
    assert queue.load() == []


def test_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    legacy = Path("/fake/data/task_queue.json")
    TaskQueue(legacy).save([_task("legacy", "from json")])
    store = StateStore(":memory:")
    store.replace_task_queue([_task("live", "already here")])
    queue = SqliteTaskQueue(store, legacy_json_path=legacy)
    loaded = queue.load()
    assert [t.task_id for t in loaded] == ["live"]


# ---------------------------------------------------------------------------
# JSON ↔ SQLite parity
# ---------------------------------------------------------------------------


def test_json_and_sqlite_backends_are_object_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same op sequence yields identical results through both backends."""
    tasks = [
        _task("t1", "first", linked_question="q1", rationale="r1"),
        _task("t2", "second", acceptance_criteria=["c1", "c2"]),
        _task("t3", "third"),
    ]
    json_q: TaskQueue = TaskQueue(Path("/fake/data/task_queue.json"))
    sqlite_q = SqliteTaskQueue(StateStore(":memory:"))

    json_q.save(tasks)
    sqlite_q.save(tasks)
    assert json_q.load() == sqlite_q.load()
    assert json_q.head() == sqlite_q.head()

    assert json_q.pop_head() == sqlite_q.pop_head()
    assert json_q.load() == sqlite_q.load()

    # Drain both fully and confirm they empty identically.
    while True:
        json_popped = json_q.pop_head()
        sqlite_popped = sqlite_q.pop_head()
        assert json_popped == sqlite_popped
        if json_popped is None:
            break
    assert json_q.load() == sqlite_q.load() == []


def test_json_and_sqlite_backends_clean_dirty_criteria_identically(
    mem_fs: dict[str, str],
) -> None:
    """Dirty ``acceptance_criteria`` round-trips identically through both.

    The JSON backend cleans on read and the SQLite backend cleans on write, so
    a directly-constructed task with blank / whitespace / non-string criteria
    yields the same canonical ``load()`` output regardless of substrate.
    """
    dirty = QueuedTask(
        task_id="t1",
        goal="real goal",
        created_at="2026-01-01T00:00:00+00:00",
        last_touched_at="2026-01-01T00:00:00+00:00",
        acceptance_criteria=["  keep  ", "", "   ", 7],  # type: ignore[list-item]
    )
    json_q = TaskQueue(Path("/fake/data/task_queue.json"))
    sqlite_q = SqliteTaskQueue(StateStore(":memory:"))
    json_q.save([dirty])
    sqlite_q.save([dirty])
    assert json_q.load() == sqlite_q.load()
    assert sqlite_q.load()[0].acceptance_criteria == ["keep"]


def test_json_and_sqlite_backends_coerce_non_string_timestamps_identically(
    mem_fs: dict[str, str],
) -> None:
    """A non-string timestamp is coerced identically by both backends.

    ``TaskQueue.load()`` defensively coerces a non-string ``created_at`` to
    ``""`` and ``last_touched_at`` to the coerced ``created_at``;
    ``normalize_task`` must mirror that so the SQLite path's ``NOT NULL``
    timestamp columns never see ``None`` (which would raise
    ``sqlite3.IntegrityError`` on ``save`` where the JSON backend tolerates and
    coerces it). Both backends' ``load()`` output must match, and the coerced
    timestamps must be empty strings, not ``None``.
    """
    dirty = QueuedTask(
        task_id="t1",
        goal="real goal",
        created_at=None,  # type: ignore[arg-type]
        last_touched_at=None,  # type: ignore[arg-type]
    )
    json_q = TaskQueue(Path("/fake/data/task_queue.json"))
    sqlite_q = SqliteTaskQueue(StateStore(":memory:"))
    # The SQLite backend must not crash on the NOT NULL columns.
    json_q.save([dirty])
    sqlite_q.save([dirty])
    assert json_q.load() == sqlite_q.load()
    coerced = sqlite_q.load()[0]
    assert coerced.created_at == ""
    assert coerced.last_touched_at == ""


def test_json_and_sqlite_backends_drop_blank_goal_identically(
    mem_fs: dict[str, str],
) -> None:
    """A blank-goal task is dropped by both backends' ``load()`` output."""
    tasks = [
        QueuedTask(
            task_id="blank",
            goal="   ",
            created_at="2026-01-01T00:00:00+00:00",
            last_touched_at="2026-01-01T00:00:00+00:00",
        ),
        _task("t2", "real"),
    ]
    json_q = TaskQueue(Path("/fake/data/task_queue.json"))
    sqlite_q = SqliteTaskQueue(StateStore(":memory:"))
    json_q.save(tasks)
    sqlite_q.save(tasks)
    assert json_q.load() == sqlite_q.load()
    assert [t.task_id for t in sqlite_q.load()] == ["t2"]


# ---------------------------------------------------------------------------
# StateStore cost-sample methods (proposal 106 Phase B Step 3)
# ---------------------------------------------------------------------------


def test_state_store_creates_cost_samples_table() -> None:
    conn = sqlite3.connect(":memory:")
    StateStore(connection=conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='cost_samples'",
    ).fetchall()
    assert len(rows) == 1


def test_append_and_load_cost_samples_round_trip() -> None:
    store = StateStore(":memory:")
    s1 = _cost(ts="2026-01-01T00:00:00+00:00", cost=1.0, caller="reflect")
    s2 = _cost(
        ts="2026-01-01T00:05:00+00:00", cost=2.0, caller="introspect",
        estimated=True, unmetered=False, provider="anthropic",
    )
    store.append_cost_sample(s1)
    store.append_cost_sample(s2)
    # Oldest-first (id ASC), every field preserved byte-for-byte.
    loaded = store.load_cost_samples_since(0.0)
    assert loaded == [s1, s2]


def test_load_cost_samples_since_honours_cutoff() -> None:
    store = StateStore(":memory:")
    old = _cost(ts="2026-01-01T00:00:00+00:00", cost=1.0)
    new = _cost(ts="2026-01-01T02:00:00+00:00", cost=2.0)
    store.append_cost_sample(old)
    store.append_cost_sample(new)
    new_epoch = datetime.fromisoformat(new.timestamp).timestamp()
    # ``>= cutoff`` boundary — the sample stamped exactly at the cutoff is kept.
    assert store.load_cost_samples_since(new_epoch) == [new]
    assert store.load_cost_samples_since(new_epoch + 1) == []
    assert store.load_cost_samples_since(0.0) == [old, new]


def test_windowed_cost_read_range_seeks_index_not_full_scan() -> None:
    """The windowed read must range-seek ``idx_cost_samples_ts_epoch`` instead
    of full-scanning the append-only, never-pruned table on the burn-rate
    throttle hot path.

    Regression guard for PR #3372: a *plain* ``ts_epoch`` index would be ignored
    because ``ORDER BY id`` is served for free by a rowid scan, so the planner
    keeps the full scan. Leading the composite index and the sort with
    ``ts_epoch`` is what makes the planner seek to the cutoff and touch only the
    in-window rows.
    """
    conn = sqlite3.connect(":memory:")
    store = StateStore(connection=conn)
    # The composite range index is registered at schema-setup time.
    index_rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND name='idx_cost_samples_ts_epoch'",
    ).fetchall()
    assert len(index_rows) == 1
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(200):
        store.append_cost_sample(
            _cost(ts=(base + timedelta(seconds=i)).isoformat()),
        )
    cutoff = (base + timedelta(seconds=180)).timestamp()
    plan = " ".join(
        row["detail"]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT timestamp, cost_usd, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, caller, "
            "duration_ms, estimated, unmetered, provider FROM cost_samples "
            "WHERE ts_epoch >= ? ORDER BY ts_epoch ASC, id ASC",
            (cutoff,),
        ).fetchall()
    )
    # The index is used to seek (SEARCH), and no full table SCAN remains.
    assert "idx_cost_samples_ts_epoch" in plan
    assert "SCAN" not in plan
    # The ORDER BY change stays oldest-first (identical to the id-ascending sort
    # under the append-only monotonic-timestamp invariant).
    window = store.load_cost_samples_since(cutoff)
    assert [s.timestamp for s in window] == sorted(s.timestamp for s in window)
    assert len(window) == 20


def test_recent_cost_samples_returns_newest_limit_oldest_first() -> None:
    store = StateStore(":memory:")
    for i in range(5):
        store.append_cost_sample(_cost(cost=float(i)))
    got = store.recent_cost_samples(3)
    assert [s.cost_usd for s in got] == [2.0, 3.0, 4.0]


def test_recent_cost_samples_non_positive_limit_returns_empty() -> None:
    store = StateStore(":memory:")
    store.append_cost_sample(_cost())
    assert store.recent_cost_samples(0) == []
    assert store.recent_cost_samples(-1) == []


def test_max_cost_sample_id_none_when_empty_then_advances() -> None:
    store = StateStore(":memory:")
    assert store.max_cost_sample_id() is None
    store.append_cost_sample(_cost())
    first = store.max_cost_sample_id()
    assert first is not None
    store.append_cost_sample(_cost())
    second = store.max_cost_sample_id()
    assert second is not None
    assert second > first


# ---------------------------------------------------------------------------
# SqliteCostSampleStore adapter
# ---------------------------------------------------------------------------


def test_sqlite_cost_sample_store_append_and_recent() -> None:
    backend = SqliteCostSampleStore(StateStore(":memory:"))
    a = _cost(ts="2026-01-01T00:00:00+00:00", cost=1.0)
    b = _cost(ts="2026-01-01T00:01:00+00:00", cost=2.0)
    backend.append(a)
    backend.append(b)
    assert backend.recent_samples(5) == [a, b]


def test_sqlite_cost_sample_store_samples_in_window_uses_now() -> None:
    backend = SqliteCostSampleStore(StateStore(":memory:"))
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    inside = _cost(ts=(now - timedelta(minutes=30)).isoformat(), cost=1.0)
    outside = _cost(ts=(now - timedelta(hours=3)).isoformat(), cost=9.0)
    backend.append(outside)
    backend.append(inside)
    assert backend.samples_in_window(1.0, now=now) == [inside]


def test_sqlite_cost_sample_store_cache_key_identity_and_version() -> None:
    backend = SqliteCostSampleStore(StateStore(":memory:"))
    identity, version = backend.cache_key()
    assert identity == f"sqlite-cost-samples:{id(backend)}"
    assert version is None  # empty store
    backend.append(_cost())
    _, version2 = backend.cache_key()
    assert version2 is not None
    # Distinct adapters never collide on the cache identity.
    other = SqliteCostSampleStore(StateStore(":memory:"))
    assert other.cache_key()[0] != identity


def test_naive_ts_epoch_derives_from_as_utc() -> None:
    """``_cost_sample_to_row`` derives ``ts_epoch`` through ``_as_utc``.

    Pins the write-path derivation to the shared naive-is-UTC owner. The value
    assertion alone cannot fail on a UTC host — there ``naive.timestamp()``
    equals ``naive.replace(tzinfo=UTC).timestamp()`` — so it is paired with a
    spy on the delegation, which a revert to a bare ``fromisoformat()`` trips
    whatever the host offset.
    """
    sample = _cost(ts="2026-03-01T09:00:00")  # naive: no offset suffix

    with patch.object(state_store, "_as_utc", wraps=_as_utc) as spy:
        row = _cost_sample_to_row(sample)

    spy.assert_called_once_with(sample.timestamp)
    assert row[1] == datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC).timestamp()


# ---------------------------------------------------------------------------
# SqliteJournalSearchIndex adapter — persistent journal-search FTS5 index
# ---------------------------------------------------------------------------

#: (corpus, query) pairs mirroring ``test_search.py``'s ``_PARITY_CASES``:
#: multi-token ranking, no-match, empty/whitespace/punctuation query, empty
#: corpus, digits/underscores, and accented word characters. The persistent
#: index must rank identically to the ephemeral ``bm25_rank`` on each.
_JOURNAL_PARITY_CORPORA: list[tuple[list[str], str]] = [
    ([], "anything"),
    (["hello world"], ""),
    (["hello world"], "   "),
    (["hello world"], "!!!"),
    (["apple pie recipe", "banana bread tutorial"], "xyzzy"),
    (["I love learning about code", "The weather is nice"], "code"),
    (
        [
            "refactor the planner module urgently",
            "refactor the authentication layer",
            "the weather is nice today",
            "update the docs for release notes",
            "the cache layer needs a quick fix",
        ],
        "refactor planner",
    ),
    (["same content here", "same content here"], "same"),
    (["", "only real doc with words", ""], "real words"),
    (["plan_42 has 3 steps", "plan_99 done", "plan_42 again"], "plan_42"),
    (["naïve approach", "a naive method", "unrelated text"], "naive"),
]


def test_state_store_creates_journal_search_table() -> None:
    """``__init__`` creates the persistent ``journal_search`` FTS5 virtual table."""
    store = StateStore(":memory:")
    row = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='journal_search'",
    ).fetchone()
    assert row is not None


@pytest.mark.parametrize("corpus, query", _JOURNAL_PARITY_CORPORA)
def test_sqlite_journal_search_matches_ephemeral_ranker(
    corpus: list[str], query: str,
) -> None:
    """The persistent index returns the same order as the ephemeral ``bm25_rank``."""
    backend = SqliteJournalSearchIndex(StateStore(":memory:"))
    assert backend.rank(corpus, query) == bm25_rank(corpus, query)


def test_sqlite_journal_search_top_up_indexes_only_the_appended_tail() -> None:
    """An append-only grow indexes only the new tail — it does not rebuild.

    Counts ``tokenize`` calls: a tail top-up tokenizes the query plus only the
    two *appended* docs (3), whereas a full rebuild would re-tokenize the query
    plus all four docs (5). The distinction is exactly the increment's point.
    """
    import maestro.state_store as state_store_mod

    store = StateStore(":memory:")
    backend = SqliteJournalSearchIndex(store)

    corpus = ["alpha one", "beta two"]
    assert backend.rank(corpus, "alpha") == bm25_rank(corpus, "alpha")
    assert store._journal_search_watermark() == 2

    grown = [*corpus, "alpha three", "gamma four"]
    expected = bm25_rank(grown, "alpha")
    with patch.object(
        state_store_mod, "tokenize", side_effect=state_store_mod.tokenize,
    ) as spy:
        assert backend.rank(grown, "alpha") == expected

    # 1 query tokenize + only the 2 appended docs (a rebuild would be 1 + 4).
    assert spy.call_count == 3
    assert store._journal_search_watermark() == 4


def test_sqlite_journal_search_repeat_over_unchanged_corpus_reindexes_nothing() -> None:
    """A second search over the same corpus performs no write at all."""
    store = StateStore(":memory:")
    backend = SqliteJournalSearchIndex(store)
    corpus = ["alpha one", "beta two", "alpha three"]
    expected = bm25_rank(corpus, "alpha")
    assert backend.rank(corpus, "alpha") == expected

    changes_before = store._conn.total_changes
    assert backend.rank(corpus, "alpha") == expected
    assert store._conn.total_changes == changes_before


def test_sqlite_journal_search_rebuilds_when_corpus_shrinks() -> None:
    """A shrunk/edited corpus (watermark > len) triggers a full rebuild."""
    store = StateStore(":memory:")
    backend = SqliteJournalSearchIndex(store)
    big = ["alpha one", "beta two", "alpha three", "gamma four"]
    assert backend.rank(big, "alpha") == bm25_rank(big, "alpha")
    assert store._journal_search_watermark() == 4

    shrunk = ["gamma solo", "alpha only"]
    assert backend.rank(shrunk, "alpha") == bm25_rank(shrunk, "alpha")
    assert backend.rank(shrunk, "alpha") == [1]
    assert store._journal_search_watermark() == 2
    # No stale rowid from the larger corpus survives the rebuild.
    rowids = [
        r["rowid"]
        for r in store._conn.execute("SELECT rowid FROM journal_search").fetchall()
    ]
    assert rowids == [0, 1]


def test_sqlite_journal_search_rebuilds_on_absent_or_garbage_watermark() -> None:
    """A missing / non-integer / negative watermark forces a rebuild, not a crash."""
    store = StateStore(":memory:")

    # Absent watermark (fresh DB): reconcile from scratch.
    assert store._journal_search_watermark() is None

    # A non-integer stamp degrades to a rebuild rather than raising. Commit the
    # direct write so it does not leave an implicit transaction open under the
    # later ``BEGIN IMMEDIATE``.
    store._conn.execute(
        "INSERT OR REPLACE INTO store_metadata (key, value) VALUES "
        "('journal_search_indexed_count_v2', 'not-a-number')",
    )
    store._conn.commit()
    assert store._journal_search_watermark() is None

    # A negative count is equally unusable.
    store._conn.execute(
        "INSERT OR REPLACE INTO store_metadata (key, value) VALUES "
        "('journal_search_indexed_count_v2', '-5')",
    )
    store._conn.commit()
    assert store._journal_search_watermark() is None

    backend = SqliteJournalSearchIndex(store)
    corpus = ["alpha one", "beta two", "alpha three"]
    assert backend.rank(corpus, "alpha") == bm25_rank(corpus, "alpha")
    assert store._journal_search_watermark() == 3


def test_sqlite_journal_search_ignores_pre_split_watermark() -> None:
    """A watermark under the unversioned pre-split key forces a full rebuild.

    The pre-split corpus held operational rows too, so its rows 0..W-1 must
    not be kept as an indexed prefix of the inner-thought-only corpus.
    """
    store = StateStore(":memory:")
    store._conn.execute("INSERT INTO journal_search (rowid, content) VALUES (0, 'stale')")
    store._conn.execute(
        "INSERT OR REPLACE INTO store_metadata (key, value) VALUES "
        "('journal_search_indexed_count', '1')",
    )
    store._conn.commit()
    assert store._journal_search_watermark() is None

    corpus = ["alpha one", "beta two"]
    assert SqliteJournalSearchIndex(store).rank(corpus, "alpha") == bm25_rank(corpus, "alpha")
    contents = [
        r["content"]
        for r in store._conn.execute("SELECT content FROM journal_search ORDER BY rowid").fetchall()
    ]
    assert contents == ["alpha one", "beta two"]


# ---------------------------------------------------------------------------
# StateStore — deed ledger table + methods
# ---------------------------------------------------------------------------


def _deed(
    *,
    deed_type: str = "merge",
    pr_number: int | None = 1,
    detail: str = "d",
    timestamp: str = "2026-01-01T00:00:00+00:00",
) -> Deed:
    """Build a fully-specified :class:`Deed` for deterministic equality."""
    return Deed(
        deed_type=deed_type,
        pr_number=pr_number,
        detail=detail,
        timestamp=timestamp,
    )


def test_state_store_creates_deeds_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='deeds'",
    )
    assert cur.fetchone() is not None


def test_append_and_recent_deeds_round_trip() -> None:
    store = StateStore(":memory:")
    a = _deed(pr_number=1, detail="a", timestamp="2026-01-01T00:00:01+00:00")
    b = _deed(pr_number=2, detail="b", timestamp="2026-01-01T00:00:02+00:00")
    store.append_deed(a)
    store.append_deed(b)
    # Newest-first (ORDER BY id DESC).
    assert store.recent_deeds(10) == [b, a]


def test_recent_deeds_honours_limit_newest_first() -> None:
    store = StateStore(":memory:")
    for i in range(5):
        store.append_deed(
            _deed(pr_number=i, timestamp=f"2026-01-01T00:00:0{i}+00:00"),
        )
    got = store.recent_deeds(2)
    assert [d.pr_number for d in got] == [4, 3]


def test_recent_deeds_non_positive_limit_returns_empty() -> None:
    store = StateStore(":memory:")
    store.append_deed(_deed())
    # SQLite treats a negative LIMIT as unbounded — the guard is load-bearing.
    assert store.recent_deeds(0) == []
    assert store.recent_deeds(-1) == []


def test_recent_deeds_pr_number_none_round_trips() -> None:
    store = StateStore(":memory:")
    store.append_deed(
        _deed(deed_type="owned_prune", pr_number=None, detail="pruned"),
    )
    got = store.recent_deeds(1)
    assert got[0].pr_number is None


def test_count_deeds_since_counts_all_without_window() -> None:
    store = StateStore(":memory:")
    store.append_deed(_deed())
    store.append_deed(_deed())
    assert store.count_deeds_since() == 2


def test_count_deeds_since_empty_store_is_zero() -> None:
    store = StateStore(":memory:")
    assert store.count_deeds_since() == 0
    assert store.count_deeds_since("2026-01-01T00:00:00+00:00") == 0


def test_count_deeds_since_windows_by_timestamp() -> None:
    store = StateStore(":memory:")
    store.append_deed(_deed(pr_number=1, timestamp="2026-07-30T00:00:00+00:00"))
    store.append_deed(_deed(pr_number=2, timestamp="2026-07-31T00:00:00+00:00"))
    store.append_deed(_deed(pr_number=3, timestamp="2026-08-01T00:00:00+00:00"))
    # since at the middle timestamp includes mid + new, excludes old.
    assert store.count_deeds_since("2026-07-31T00:00:00+00:00") == 2
    assert store.count_deeds_since("2026-09-01T00:00:00+00:00") == 0
    assert store.count_deeds_since("2026-01-01T00:00:00+00:00") == 3


def test_seed_deeds_once_bulk_insert_preserves_chronology() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _deed(pr_number=1, detail="old", timestamp="2026-01-01T00:00:01+00:00"),
        _deed(pr_number=2, detail="mid", timestamp="2026-01-01T00:00:02+00:00"),
        _deed(pr_number=3, detail="new", timestamp="2026-01-01T00:00:03+00:00"),
    ]
    store.seed_deeds_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id DESC is
    # newest-first — matching the JSONL reverse walk.
    assert [d.pr_number for d in store.recent_deeds(10)] == [3, 2, 1]
    assert store.count_deeds_since() == 3
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_deed_ledger_seeded() is True


def test_seed_deeds_once_empty_list_stamps_marker_without_rows() -> None:
    store = StateStore(":memory:")
    store.seed_deeds_once([])
    assert store.count_deeds_since() == 0
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_deed_ledger_seeded() is True


def test_seed_deeds_once_is_idempotent_and_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops.

    Regression for the append-only doubling race (proposal 106): the guarded
    seed re-reads the durable marker inside its ``BEGIN IMMEDIATE`` critical
    section, so a repeat call — the single-connection analogue of a second
    process racing the first seed — inserts nothing.
    """
    store = StateStore(":memory:")
    legacy = [
        _deed(pr_number=1, detail="a", timestamp="2026-01-01T00:00:01+00:00"),
        _deed(pr_number=2, detail="b", timestamp="2026-01-01T00:00:02+00:00"),
    ]
    store.seed_deeds_once(legacy)
    store.seed_deeds_once(legacy)
    assert store.count_deeds_since() == 2


def test_seed_deeds_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_deeds_once([])  # empty seed stamps the durable marker
    store.seed_deeds_once(
        [_deed(pr_number=1, detail="a", timestamp="2026-01-01T00:00:01+00:00")],
    )
    assert store.count_deeds_since() == 0


def test_seed_deeds_once_does_not_double_populated_unmarked_store() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_deed(
        _deed(pr_number=9, detail="already", timestamp="2026-01-01T00:00:00+00:00"),
    )
    store.seed_deeds_once(
        [_deed(pr_number=1, detail="a", timestamp="2026-01-01T00:00:01+00:00")],
    )
    assert store.count_deeds_since() == 1
    assert [d.pr_number for d in store.recent_deeds(10)] == [9]
    assert store.is_deed_ledger_seeded() is True


# ---------------------------------------------------------------------------
# StateStore — deed-ledger seed marker
# ---------------------------------------------------------------------------


def test_deed_ledger_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_deeds_once`` still stamps the durable seed marker."""
    store = StateStore(":memory:")
    assert store.is_deed_ledger_seeded() is False
    store.seed_deeds_once([])
    assert store.is_deed_ledger_seeded() is True


# ---------------------------------------------------------------------------
# SqliteDeedLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------

_LEGACY_DIR = Path("/fake/data")


def _seed_legacy(*deeds: tuple[DeedType, int | None, str, str]) -> None:
    """Append legacy deeds to /fake/data/deed_ledger.jsonl (oldest-first)."""
    for deed_type, pr, detail, ts in deeds:
        record_deed(_LEGACY_DIR, deed_type, pr, detail, now=ts)


def test_deed_adapter_seeds_from_legacy_jsonl(mem_fs: dict[str, str]) -> None:
    _seed_legacy(
        (DeedType.PR_CLOSE, 1, "first", "2026-01-01T00:00:01+00:00"),
        (DeedType.MERGE, 2, "second", "2026-01-01T00:00:02+00:00"),
    )
    ledger = SqliteDeedLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    # Newest-first, and count carried the full history over.
    assert [d.pr_number for d in ledger.recent(10)] == [2, 1]
    assert ledger.count() == 2


def test_deed_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy((DeedType.MERGE, 1, "first", "2026-01-01T00:00:01+00:00"))
    store = StateStore(":memory:")
    first = SqliteDeedLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert first.count() == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteDeedLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert second.count() == 1


def test_deed_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy((DeedType.MERGE, 1, "first", "2026-01-01T00:00:01+00:00"))
    ledger = SqliteDeedLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.count() == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert ledger.count() == 1


def test_deed_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present.

    Regression for the durable seed marker: a fresh adapter's in-memory guard is
    reset on restart, so only the persisted marker prevents re-seeding an empty
    store from the still-present legacy JSONL (which would resurrect the count).
    """
    _seed_legacy((DeedType.MERGE, 1, "first", "2026-01-01T00:00:01+00:00"))
    store = StateStore(":memory:")
    store.seed_deeds_once([])  # empty seed stamps the durable marker
    ledger = SqliteDeedLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.count() == 0  # marker blocks the re-seed
    assert store.is_deed_ledger_seeded() is True


def test_deed_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteDeedLedger(StateStore(":memory:"), legacy_jsonl_dir=None)
    assert ledger.count() == 0


def test_deed_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteDeedLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.count() == 0


def test_deed_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / DEED_LEDGER_FILENAME)] = ""
    ledger = SqliteDeedLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.count() == 0


def test_deed_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy((DeedType.MERGE, 99, "from legacy", "2026-01-01T00:00:01+00:00"))
    store = StateStore(":memory:")
    store.append_deed(_deed(pr_number=1, detail="already here"))
    ledger = SqliteDeedLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    got = ledger.recent(10)
    assert [d.pr_number for d in got] == [1]


# ---------------------------------------------------------------------------
# StateStore — lever-attempts ledger (proposal 106)
# ---------------------------------------------------------------------------


def _attempt(
    lever: str = "mark_plan_obsolete",
    args: list[str] | None = None,
    verified: bool = True,
    result: bool = True,
) -> LeverAttempt:
    return build_lever_attempt(
        lever, args if args is not None else ["p", "reason"], verified, result,
    )


def _seed_legacy_attempts(*attempts: LeverAttempt) -> None:
    """Append legacy attempts to /fake/data/lever_attempts.jsonl (oldest-first)."""
    ledger = JsonlLeverAttemptsLedger(_LEGACY_DIR)
    for attempt in attempts:
        ledger.record(attempt)


def test_state_store_creates_lever_attempts_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='lever_attempts'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_lever_attempts_round_trip() -> None:
    store = StateStore(":memory:")
    a = _attempt(args=["a", "r"], verified=True, result=True)
    b = _attempt(args=["b", "r"], verified=False, result=False)
    store.append_lever_attempt(a)
    store.append_lever_attempt(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order, and both the
    # JSON-encoded args and the 0/1 boolean flags round-trip.
    assert store.load_lever_attempts() == [a, b]


def test_load_lever_attempts_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_lever_attempts() == []


def test_seed_lever_attempts_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _attempt(args=["old", "r"]),
        _attempt(args=["mid", "r"]),
        _attempt(args=["new", "r"]),
    ]
    store.seed_lever_attempts_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id ASC is
    # oldest-first — matching the JSONL forward walk.
    assert [at.args[0] for at in store.load_lever_attempts()] == [
        "old", "mid", "new",
    ]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_lever_attempts_seeded() is True


def test_seed_lever_attempts_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_lever_attempts_once([])
    assert store.load_lever_attempts() == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_lever_attempts_seeded() is True


def test_seed_lever_attempts_once_idempotent_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops.

    Regression for the append-only doubling race (proposal 106): the guarded
    seed re-reads the durable marker inside its ``BEGIN IMMEDIATE`` critical
    section, so a repeat call inserts nothing.
    """
    store = StateStore(":memory:")
    legacy = [_attempt(args=["a", "r"]), _attempt(args=["b", "r"])]
    store.seed_lever_attempts_once(legacy)
    store.seed_lever_attempts_once(legacy)
    assert len(store.load_lever_attempts()) == 2


def test_seed_lever_attempts_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_lever_attempts_once([])  # empty seed stamps the durable marker
    store.seed_lever_attempts_once([_attempt(args=["a", "r"])])
    assert store.load_lever_attempts() == []


def test_seed_lever_attempts_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_lever_attempt(_attempt(args=["already", "r"]))
    store.seed_lever_attempts_once([_attempt(args=["a", "r"])])
    loaded = store.load_lever_attempts()
    assert len(loaded) == 1
    assert loaded[0].args[0] == "already"
    assert store.is_lever_attempts_seeded() is True


def test_lever_attempts_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_lever_attempts_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_lever_attempts_seeded() is False
    store.seed_lever_attempts_once([])
    assert store.is_lever_attempts_seeded() is True


# ---------------------------------------------------------------------------
# SqliteLeverAttemptsLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------


def test_lever_adapter_seeds_from_legacy_jsonl(mem_fs: dict[str, str]) -> None:
    _seed_legacy_attempts(
        _attempt(args=["aaa", "r"], result=True),
        _attempt(args=["bbb", "r"], result=False),
    )
    ledger = SqliteLeverAttemptsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    # Oldest-first, and the full history carried over.
    assert [at.args[0] for at in ledger.load_all()] == ["aaa", "bbb"]


def test_lever_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_attempts(_attempt(args=["aaa", "r"]))
    store = StateStore(":memory:")
    first = SqliteLeverAttemptsLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.load_all()) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteLeverAttemptsLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.load_all()) == 1


def test_lever_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_attempts(_attempt(args=["aaa", "r"]))
    ledger = SqliteLeverAttemptsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.load_all()) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.load_all()) == 1


def test_lever_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_attempts(_attempt(args=["aaa", "r"]))
    store = StateStore(":memory:")
    store.seed_lever_attempts_once([])  # empty seed stamps the durable marker
    ledger = SqliteLeverAttemptsLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.load_all() == []  # marker blocks the re-seed
    assert store.is_lever_attempts_seeded() is True


def test_lever_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteLeverAttemptsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.load_all() == []


def test_lever_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteLeverAttemptsLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.load_all() == []


def test_lever_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / LEVER_ATTEMPTS_FILENAME)] = ""
    ledger = SqliteLeverAttemptsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.load_all() == []


def test_lever_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_attempts(_attempt(args=["from_legacy", "r"]))
    store = StateStore(":memory:")
    store.append_lever_attempt(_attempt(args=["already", "r"]))
    ledger = SqliteLeverAttemptsLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert [at.args[0] for at in ledger.load_all()] == ["already"]


def test_lever_adapter_record_persists() -> None:
    ledger = SqliteLeverAttemptsLedger(StateStore(":memory:"))
    ledger.record(_attempt(args=["p", "r"], result=True))
    loaded = ledger.load_all()
    assert len(loaded) == 1
    assert loaded[0].args[0] == "p"


# ---------------------------------------------------------------------------
# audit_history table — round-trip, seed order, idempotency (proposal 106)
# ---------------------------------------------------------------------------


def _audit_result(
    iteration: int = 1,
    timestamp: str = "2026-08-15T00:00:00Z",
    findings: list[AuditFinding] | None = None,
    rules_fired: list[str] | None = None,
) -> AuditResult:
    return AuditResult(
        iteration=iteration,
        timestamp=timestamp,
        findings=findings or [],
        rules_fired=rules_fired or [],
    )


def _seed_legacy_audit(*results: AuditResult) -> None:
    """Append legacy audit results to /fake/data/audit_history.jsonl (oldest-first)."""
    ledger = JsonlAuditHistoryLedger(_LEGACY_DIR)
    for result in results:
        ledger.record(result)


def test_state_store_creates_audit_history_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='audit_history'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_audit_results_round_trip() -> None:
    store = StateStore(":memory:")
    a = _audit_result(
        iteration=1,
        findings=[
            AuditFinding(
                kind="contradicted_claim",
                statement="s1",
                cites=["t1", "t2"],
                rule="success_claim_vs_rate",
            ),
        ],
        rules_fired=["success_claim_vs_rate"],
    )
    b = _audit_result(iteration=2, timestamp="2026-08-15T01:00:00Z")
    store.append_audit_result(a)
    store.append_audit_result(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order, and the whole
    # result — including the nested findings list — round-trips via the JSON
    # blob.
    assert store.load_audit_results() == [a, b]


def test_load_audit_results_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_audit_results() == []


def test_load_audit_results_hydrates_with_field_filter() -> None:
    """A blob with an unknown key and an invalid-kind finding hydrates cleanly.

    The SQLite reader shares the JSONL core (:func:`_parse_audit_dict`): it drops
    top-level keys outside ``fields(AuditResult)`` and per-finding fields outside
    ``fields(AuditFinding)``, so a blob written by a newer schema (extra keys)
    round-trips through the known fields.
    """
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO audit_history (data) VALUES (?)",
        (json.dumps({
            "iteration": 3,
            "timestamp": "ts3",
            "findings": [
                {
                    "kind": "contradicted_claim",
                    "statement": "kept",
                    "cites": ["c1"],
                    "rule": "r",
                    "unknown_finding_field": "ignored",  # dropped
                },
            ],
            "rules_fired": ["r"],
            "unknown_future_field": "ignored",  # unknown top-level key -> dropped
        }),),
    )
    store._conn.commit()
    loaded = store.load_audit_results()
    assert len(loaded) == 1
    assert loaded[0].iteration == 3
    assert len(loaded[0].findings) == 1
    assert loaded[0].findings[0].statement == "kept"
    assert loaded[0].rules_fired == ["r"]


def test_seed_audit_history_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _audit_result(iteration=1),
        _audit_result(iteration=2),
        _audit_result(iteration=3),
    ]
    store.seed_audit_history_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id ASC is
    # oldest-first — matching the JSONL forward walk.
    assert [r.iteration for r in store.load_audit_results()] == [1, 2, 3]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_audit_history_seeded() is True


def test_seed_audit_history_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_audit_history_once([])
    assert store.load_audit_results() == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_audit_history_seeded() is True


def test_seed_audit_history_once_idempotent_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [_audit_result(iteration=1), _audit_result(iteration=2)]
    store.seed_audit_history_once(legacy)
    store.seed_audit_history_once(legacy)
    assert len(store.load_audit_results()) == 2


def test_seed_audit_history_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_audit_history_once([])  # empty seed stamps the durable marker
    store.seed_audit_history_once([_audit_result(iteration=9)])
    assert store.load_audit_results() == []


def test_seed_audit_history_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_audit_result(_audit_result(iteration=1))
    store.seed_audit_history_once([_audit_result(iteration=2)])
    loaded = store.load_audit_results()
    assert len(loaded) == 1
    assert loaded[0].iteration == 1
    assert store.is_audit_history_seeded() is True


def test_audit_history_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_audit_history_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_audit_history_seeded() is False
    store.seed_audit_history_once([])
    assert store.is_audit_history_seeded() is True


# ---------------------------------------------------------------------------
# SqliteAuditHistoryLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------


def test_audit_adapter_seeds_from_legacy_jsonl(mem_fs: dict[str, str]) -> None:
    _seed_legacy_audit(
        _audit_result(iteration=1),
        _audit_result(iteration=2),
    )
    ledger = SqliteAuditHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    # Oldest-first, and the full history carried over.
    assert [r.iteration for r in ledger.load_all()] == [1, 2]


def test_audit_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_audit(_audit_result(iteration=1))
    store = StateStore(":memory:")
    first = SqliteAuditHistoryLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.load_all()) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteAuditHistoryLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.load_all()) == 1


def test_audit_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_audit(_audit_result(iteration=1))
    ledger = SqliteAuditHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.load_all()) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.load_all()) == 1


def test_audit_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_audit(_audit_result(iteration=1))
    store = StateStore(":memory:")
    store.seed_audit_history_once([])  # empty seed stamps the durable marker
    ledger = SqliteAuditHistoryLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.load_all() == []  # marker blocks the re-seed
    assert store.is_audit_history_seeded() is True


def test_audit_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteAuditHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.load_all() == []


def test_audit_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteAuditHistoryLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.load_all() == []


def test_audit_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / AUDIT_HISTORY_FILENAME)] = ""
    ledger = SqliteAuditHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.load_all() == []


def test_audit_adapter_record_persists() -> None:
    ledger = SqliteAuditHistoryLedger(StateStore(":memory:"))
    ledger.record(_audit_result(iteration=7))
    loaded = ledger.load_all()
    assert len(loaded) == 1
    assert loaded[0].iteration == 7


# ---------------------------------------------------------------------------
# rejection_history table — round-trip, seed order, idempotency (proposal 106)
# ---------------------------------------------------------------------------


def _rejection(**overrides: Any) -> RejectionRecord:
    defaults: dict[str, Any] = dict(
        plan_id="ep_1",
        title="Example plan",
        category="capability",
        priority="capability",
        rejection_reason="Too aggressive",
        timestamp="2026-04-23T12:00:00+00:00",
        rejection_category="scope_too_large",
    )
    defaults.update(overrides)
    return RejectionRecord(**defaults)


def _seed_legacy_rejections(*records: RejectionRecord) -> None:
    """Append legacy rejections to /fake/data/rejection_history.jsonl (oldest-first)."""
    ledger = JsonlRejectionHistoryLedger(_LEGACY_DIR)
    for record in records:
        ledger.record(record)


def test_state_store_creates_rejection_history_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='rejection_history'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_rejections_round_trip() -> None:
    store = StateStore(":memory:")
    a = _rejection(plan_id="ep_1")
    b = _rejection(plan_id="ep_2")
    store.append_rejection(a)
    store.append_rejection(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order, and the whole
    # record round-trips via the JSON blob.
    assert store.load_rejections() == [a, b]


def test_load_rejections_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_rejections() == []


def test_load_rejections_hydrates_with_field_filter() -> None:
    """A blob with an unknown key and a legacy priority hydrates cleanly.

    The SQLite reader shares the JSONL core (:func:`_parse_rejection_dict`): it
    drops keys outside ``fields(RejectionRecord)`` and coerces a legacy
    ``priority`` string via ``LEGACY_PRIORITY_REMAP``, so a blob written by a
    newer schema (extra keys) or an older one round-trips through the known
    fields.
    """
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO rejection_history (data) VALUES (?)",
        (json.dumps({
            "plan_id": "ep_legacy",
            "title": "old",
            "category": "capability",
            "priority": "bug_fix",  # legacy -> remapped to "resilience"
            "rejection_reason": "no",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "rejection_category": "other",
            "unknown_future_field": "ignored",  # dropped
        }),),
    )
    store._conn.commit()
    loaded = store.load_rejections()
    assert len(loaded) == 1
    assert loaded[0].plan_id == "ep_legacy"
    assert loaded[0].priority == "resilience"


def test_seed_rejection_history_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _rejection(plan_id="ep_1"),
        _rejection(plan_id="ep_2"),
        _rejection(plan_id="ep_3"),
    ]
    store.seed_rejection_history_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id ASC is
    # oldest-first — matching the JSONL forward walk.
    assert [r.plan_id for r in store.load_rejections()] == ["ep_1", "ep_2", "ep_3"]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_rejection_history_seeded() is True


def test_seed_rejection_history_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_rejection_history_once([])
    assert store.load_rejections() == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_rejection_history_seeded() is True


def test_seed_rejection_history_once_idempotent_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [_rejection(plan_id="ep_1"), _rejection(plan_id="ep_2")]
    store.seed_rejection_history_once(legacy)
    store.seed_rejection_history_once(legacy)
    assert len(store.load_rejections()) == 2


def test_seed_rejection_history_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_rejection_history_once([])  # empty seed stamps the durable marker
    store.seed_rejection_history_once([_rejection(plan_id="ep_9")])
    assert store.load_rejections() == []


def test_seed_rejection_history_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_rejection(_rejection(plan_id="ep_1"))
    store.seed_rejection_history_once([_rejection(plan_id="ep_2")])
    loaded = store.load_rejections()
    assert len(loaded) == 1
    assert loaded[0].plan_id == "ep_1"
    assert store.is_rejection_history_seeded() is True


def test_rejection_history_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_rejection_history_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_rejection_history_seeded() is False
    store.seed_rejection_history_once([])
    assert store.is_rejection_history_seeded() is True


# ---------------------------------------------------------------------------
# SqliteRejectionHistoryLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------


def test_rejection_adapter_seeds_from_legacy_jsonl(mem_fs: dict[str, str]) -> None:
    _seed_legacy_rejections(
        _rejection(plan_id="ep_1"),
        _rejection(plan_id="ep_2"),
    )
    ledger = SqliteRejectionHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    # Oldest-first, and the full history carried over.
    assert [r.plan_id for r in ledger.load_all()] == ["ep_1", "ep_2"]


def test_rejection_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_rejections(_rejection(plan_id="ep_1"))
    store = StateStore(":memory:")
    first = SqliteRejectionHistoryLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.load_all()) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteRejectionHistoryLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.load_all()) == 1


def test_rejection_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_rejections(_rejection(plan_id="ep_1"))
    ledger = SqliteRejectionHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.load_all()) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.load_all()) == 1


def test_rejection_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_rejections(_rejection(plan_id="ep_1"))
    store = StateStore(":memory:")
    store.seed_rejection_history_once([])  # empty seed stamps the durable marker
    ledger = SqliteRejectionHistoryLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.load_all() == []  # marker blocks the re-seed
    assert store.is_rejection_history_seeded() is True


def test_rejection_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteRejectionHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.load_all() == []


def test_rejection_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteRejectionHistoryLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.load_all() == []


def test_rejection_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / REJECTION_HISTORY_FILENAME)] = ""
    ledger = SqliteRejectionHistoryLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.load_all() == []


def test_rejection_adapter_record_persists() -> None:
    ledger = SqliteRejectionHistoryLedger(StateStore(":memory:"))
    ledger.record(_rejection(plan_id="ep_7"))
    loaded = ledger.load_all()
    assert len(loaded) == 1
    assert loaded[0].plan_id == "ep_7"


def test_rejection_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """JSONL and SQLite adapters return identical ``RejectionRecord`` lists.

    A shared corpus (including a canonical-priority row) round-trips identically
    through both substrates, so flipping ``[paths] sqlite_state_enabled`` never
    changes what the rejection readers see — the core proposal-106 parity
    guarantee.
    """
    corpus = [
        _rejection(plan_id="ep_1"),
        _rejection(plan_id="ep_2", priority="resilience"),
    ]
    jsonl = JsonlRejectionHistoryLedger(Path("/fake/parity-rej"))
    sqlite = SqliteRejectionHistoryLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.record(record)
    assert jsonl.load_all() == corpus
    assert sqlite.load_all() == corpus
    assert jsonl.load_all() == sqlite.load_all()


# ---------------------------------------------------------------------------
# version_archive table — round-trip, seed order, idempotency (proposal 106)
# ---------------------------------------------------------------------------


def _version(**overrides: Any) -> VersionRecord:
    defaults: dict[str, Any] = dict(
        version_id="v_1",
        plan_id="ep_1",
        plan_title="Example plan",
        commit_sha="abc123",
        merged_at="2026-04-23T12:00:00+00:00",
        window_tasks_total=5,
        window_tasks_succeeded=4,
        window_tasks_failed=1,
        window_sdk_cost_usd=1.25,
        window_sdk_calls=10,
        window_cache_hit_rate=0.5,
        window_rate_limit_hits=0,
        window_error_count=0,
        quality_gate_pass=True,
        notes=None,
    )
    defaults.update(overrides)
    return VersionRecord(**defaults)


def _seed_legacy_versions(*records: VersionRecord) -> None:
    """Append legacy versions to /fake/data/version_archive.jsonl (oldest-first)."""
    ledger = JsonlVersionArchiveLedger(_LEGACY_DIR)
    for record in records:
        ledger.record(record)


def test_state_store_creates_version_archive_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='version_archive'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_versions_round_trip() -> None:
    store = StateStore(":memory:")
    a = _version(version_id="v_1", plan_id="ep_1")
    b = _version(version_id="v_2", plan_id="ep_2")
    store.append_version(a)
    store.append_version(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order, and the whole
    # record round-trips via the JSON blob.
    assert store.load_versions() == [a, b]


def test_load_versions_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_versions() == []


def test_load_versions_hydrates_with_field_filter() -> None:
    """A blob with an unknown future key hydrates through the known fields.

    The SQLite reader shares the JSONL core (:meth:`Memory._parse_version_dict`):
    it drops keys outside ``fields(VersionRecord)``, so a blob written by a newer
    schema (extra keys) round-trips through the known fields verbatim.
    """
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO version_archive (data) VALUES (?)",
        (json.dumps({
            "version_id": "v_extra",
            "plan_id": "ep_extra",
            "plan_title": "old",
            "commit_sha": "deadbeef",
            "merged_at": "2026-04-23T00:00:00+00:00",
            "window_tasks_total": 1,
            "window_tasks_succeeded": 1,
            "window_tasks_failed": 0,
            "window_sdk_cost_usd": 0.0,
            "window_sdk_calls": 1,
            "window_cache_hit_rate": 1.0,
            "window_rate_limit_hits": 0,
            "window_error_count": 0,
            "quality_gate_pass": True,
            "notes": "kept",
            "unknown_future_field": "ignored",  # dropped
        }),),
    )
    store._conn.commit()
    loaded = store.load_versions()
    assert len(loaded) == 1
    assert loaded[0].version_id == "v_extra"
    assert loaded[0].notes == "kept"


def test_load_versions_drops_structurally_corrupt_blob() -> None:
    """A blob missing required fields is dropped, not raised.

    ``_parse_version_dict`` returns ``None`` on the ``TypeError`` from a row
    missing a required field, so the corrupt row is skipped exactly as the JSONL
    path skips it — a well-formed neighbour still loads.
    """
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO version_archive (data) VALUES (?)",
        (json.dumps({"plan_id": "ep_missing_fields"}),),  # no version_id etc.
    )
    store.append_version(_version(version_id="v_ok", plan_id="ep_ok"))
    loaded = store.load_versions()
    assert [v.version_id for v in loaded] == ["v_ok"]


def test_seed_version_archive_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _version(version_id="v_1"),
        _version(version_id="v_2"),
        _version(version_id="v_3"),
    ]
    store.seed_version_archive_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id ASC is
    # oldest-first — matching the JSONL forward walk.
    assert [v.version_id for v in store.load_versions()] == ["v_1", "v_2", "v_3"]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_version_archive_seeded() is True


def test_seed_version_archive_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_version_archive_once([])
    assert store.load_versions() == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_version_archive_seeded() is True


def test_seed_version_archive_once_idempotent_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [_version(version_id="v_1"), _version(version_id="v_2")]
    store.seed_version_archive_once(legacy)
    store.seed_version_archive_once(legacy)
    assert len(store.load_versions()) == 2


def test_seed_version_archive_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_version_archive_once([])  # empty seed stamps the durable marker
    store.seed_version_archive_once([_version(version_id="v_9")])
    assert store.load_versions() == []


def test_seed_version_archive_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_version(_version(version_id="v_1"))
    store.seed_version_archive_once([_version(version_id="v_2")])
    loaded = store.load_versions()
    assert len(loaded) == 1
    assert loaded[0].version_id == "v_1"
    assert store.is_version_archive_seeded() is True


def test_version_archive_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_version_archive_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_version_archive_seeded() is False
    store.seed_version_archive_once([])
    assert store.is_version_archive_seeded() is True


# ---------------------------------------------------------------------------
# SqliteVersionArchiveLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------


def test_version_adapter_seeds_from_legacy_jsonl(mem_fs: dict[str, str]) -> None:
    _seed_legacy_versions(
        _version(version_id="v_1"),
        _version(version_id="v_2"),
    )
    ledger = SqliteVersionArchiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    # Oldest-first, and the full archive carried over.
    assert [v.version_id for v in ledger.load_all()] == ["v_1", "v_2"]


def test_version_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_versions(_version(version_id="v_1"))
    store = StateStore(":memory:")
    first = SqliteVersionArchiveLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.load_all()) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteVersionArchiveLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.load_all()) == 1


def test_version_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_versions(_version(version_id="v_1"))
    ledger = SqliteVersionArchiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.load_all()) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.load_all()) == 1


def test_version_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_versions(_version(version_id="v_1"))
    store = StateStore(":memory:")
    store.seed_version_archive_once([])  # empty seed stamps the durable marker
    ledger = SqliteVersionArchiveLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.load_all() == []  # marker blocks the re-seed
    assert store.is_version_archive_seeded() is True


def test_version_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteVersionArchiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.load_all() == []


def test_version_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteVersionArchiveLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.load_all() == []


def test_version_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / VERSION_ARCHIVE_FILENAME)] = ""
    ledger = SqliteVersionArchiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.load_all() == []


def test_version_adapter_record_persists() -> None:
    ledger = SqliteVersionArchiveLedger(StateStore(":memory:"))
    ledger.record(_version(version_id="v_7"))
    loaded = ledger.load_all()
    assert len(loaded) == 1
    assert loaded[0].version_id == "v_7"


def test_version_archive_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """JSONL and SQLite adapters return identical ``VersionRecord`` lists.

    A shared corpus (including a row carrying ``notes``) round-trips identically
    through both substrates, so flipping ``[paths] sqlite_state_enabled`` never
    changes what the three version readers see — the core proposal-106 parity
    guarantee.
    """
    corpus = [
        _version(version_id="v_1", plan_id="ep_1"),
        _version(version_id="v_2", plan_id="ep_2", notes="reconciled"),
    ]
    jsonl = JsonlVersionArchiveLedger(Path("/fake/parity-ver"))
    sqlite = SqliteVersionArchiveLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.record(record)
    assert jsonl.load_all() == corpus
    assert sqlite.load_all() == corpus
    assert jsonl.load_all() == sqlite.load_all()


# ---------------------------------------------------------------------------
# question_resolutions table + seed-once + SqliteQuestionResolutionLedger
# ---------------------------------------------------------------------------


def _question_resolution(**overrides: Any) -> QuestionResolution:
    defaults: dict[str, Any] = dict(
        resolution_id="r1",
        iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question="What am I capable of?",
        task_id="t1",
        outcome="success",
        disposition="removed",
        finding="I can refactor multi-file changes.",
        refined_to=None,
    )
    defaults.update(overrides)
    return QuestionResolution(**defaults)


def _seed_legacy_question_resolutions(*records: QuestionResolution) -> None:
    """Append legacy rows to /fake/data/question_resolutions.jsonl (oldest-first)."""
    ledger = JsonlQuestionResolutionLedger(_LEGACY_DIR)
    for record in records:
        ledger.record(record)


def test_state_store_creates_question_resolutions_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='question_resolutions'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_question_resolutions_round_trip() -> None:
    store = StateStore(":memory:")
    a = _question_resolution(resolution_id="r1")
    b = _question_resolution(resolution_id="r2")
    store.append_question_resolution(a)
    store.append_question_resolution(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order, and the whole
    # record round-trips via the JSON blob.
    assert store.load_question_resolutions() == [a, b]


def test_load_question_resolutions_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_question_resolutions() == []


def test_load_question_resolutions_hydrates_with_field_filter() -> None:
    """A blob with an unknown key hydrates cleanly (dropped, not raised).

    The SQLite reader shares the JSONL core
    (:func:`_parse_question_resolution_dict`): it drops keys outside
    ``fields(QuestionResolution)`` — and, unlike the rejection core, applies no
    ``LEGACY_PRIORITY_REMAP`` — so a blob written by a newer schema (extra keys)
    round-trips through the known fields.
    """
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO question_resolutions (data) VALUES (?)",
        (json.dumps({
            "resolution_id": "r_legacy",
            "iteration": 3,
            "timestamp": "2026-04-26T00:00:00+00:00",
            "question": "old",
            "task_id": "t9",
            "outcome": "success",
            "disposition": "kept",
            "finding": "f",
            "unknown_future_field": "ignored",  # dropped
        }),),
    )
    store._conn.commit()
    loaded = store.load_question_resolutions()
    assert len(loaded) == 1
    assert loaded[0].resolution_id == "r_legacy"
    assert loaded[0].refined_to is None


def test_load_question_resolutions_skips_non_json_blob() -> None:
    """A blob that is not valid JSON is skipped, not raised.

    The JSONL walk (:func:`~maestro.memory.load_all_question_resolutions`)
    warns-and-skips a malformed line, and callers treat this read as
    storage-level — the audit-stall task gate softens only
    ``OSError``/``sqlite3.Error``, so a ``json.JSONDecodeError`` escaping here
    would crash task generation on one corrupt row.
    """
    store = StateStore(":memory:")
    good = _question_resolution(resolution_id="r_ok")
    store._conn.execute(
        "INSERT INTO question_resolutions (data) VALUES (?)",
        ("{not json",),
    )
    store.append_question_resolution(good)
    store._conn.commit()
    assert store.load_question_resolutions() == [good]


def test_load_question_resolutions_skips_non_object_blob() -> None:
    """A JSON scalar blob is skipped too (``TypeError`` from the hydrator)."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO question_resolutions (data) VALUES (?)",
        (json.dumps("just-a-string"),),
    )
    store._conn.commit()
    assert store.load_question_resolutions() == []


def test_seed_question_resolutions_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _question_resolution(resolution_id="r1"),
        _question_resolution(resolution_id="r2"),
        _question_resolution(resolution_id="r3"),
    ]
    store.seed_question_resolutions_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id ASC is
    # oldest-first — matching the JSONL forward walk.
    assert [
        r.resolution_id for r in store.load_question_resolutions()
    ] == ["r1", "r2", "r3"]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_question_resolutions_seeded() is True


def test_seed_question_resolutions_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_question_resolutions_once([])
    assert store.load_question_resolutions() == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_question_resolutions_seeded() is True


def test_seed_question_resolutions_once_idempotent_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [
        _question_resolution(resolution_id="r1"),
        _question_resolution(resolution_id="r2"),
    ]
    store.seed_question_resolutions_once(legacy)
    store.seed_question_resolutions_once(legacy)
    assert len(store.load_question_resolutions()) == 2


def test_seed_question_resolutions_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_question_resolutions_once([])  # empty seed stamps the durable marker
    store.seed_question_resolutions_once([_question_resolution(resolution_id="r9")])
    assert store.load_question_resolutions() == []


def test_seed_question_resolutions_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_question_resolution(_question_resolution(resolution_id="r1"))
    store.seed_question_resolutions_once([_question_resolution(resolution_id="r2")])
    loaded = store.load_question_resolutions()
    assert len(loaded) == 1
    assert loaded[0].resolution_id == "r1"
    assert store.is_question_resolutions_seeded() is True


def test_question_resolutions_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_question_resolutions_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_question_resolutions_seeded() is False
    store.seed_question_resolutions_once([])
    assert store.is_question_resolutions_seeded() is True


def test_question_resolution_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_question_resolutions(
        _question_resolution(resolution_id="r1"),
        _question_resolution(resolution_id="r2"),
    )
    ledger = SqliteQuestionResolutionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    # Oldest-first, and the full history carried over.
    assert [
        r.resolution_id for r in ledger.load_all()
    ] == ["r1", "r2"]


def test_question_resolution_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_question_resolutions(_question_resolution(resolution_id="r1"))
    store = StateStore(":memory:")
    first = SqliteQuestionResolutionLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.load_all()) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteQuestionResolutionLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.load_all()) == 1


def test_question_resolution_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_question_resolutions(_question_resolution(resolution_id="r1"))
    ledger = SqliteQuestionResolutionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.load_all()) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.load_all()) == 1


def test_question_resolution_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_question_resolutions(_question_resolution(resolution_id="r1"))
    store = StateStore(":memory:")
    store.seed_question_resolutions_once([])  # empty seed stamps the durable marker
    ledger = SqliteQuestionResolutionLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.load_all() == []  # marker blocks the re-seed
    assert store.is_question_resolutions_seeded() is True


def test_question_resolution_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteQuestionResolutionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.load_all() == []


def test_question_resolution_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteQuestionResolutionLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.load_all() == []


def test_question_resolution_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / QUESTION_RESOLUTIONS_FILENAME)] = ""
    ledger = SqliteQuestionResolutionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.load_all() == []


def test_question_resolution_adapter_record_persists() -> None:
    ledger = SqliteQuestionResolutionLedger(StateStore(":memory:"))
    ledger.record(_question_resolution(resolution_id="r7"))
    loaded = ledger.load_all()
    assert len(loaded) == 1
    assert loaded[0].resolution_id == "r7"


def test_question_resolution_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """JSONL and SQLite adapters return identical ``QuestionResolution`` lists.

    A shared corpus (including a ``refined`` row carrying ``refined_to``)
    round-trips identically through both substrates, so flipping
    ``[paths] sqlite_state_enabled`` never changes what the question-resolution
    readers see — the core proposal-106 parity guarantee.
    """
    corpus = [
        _question_resolution(resolution_id="r1"),
        _question_resolution(
            resolution_id="r2",
            disposition="refined",
            refined_to="A sharper successor question?",
        ),
    ]
    jsonl = JsonlQuestionResolutionLedger(Path("/fake/parity-qr"))
    sqlite = SqliteQuestionResolutionLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.record(record)
    assert jsonl.load_all() == corpus
    assert sqlite.load_all() == corpus
    assert jsonl.load_all() == sqlite.load_all()


# ---------------------------------------------------------------------------
# gate_activations table + seed-once + SqliteGateActivationLedger
# ---------------------------------------------------------------------------


def _gate_activation(**overrides: Any) -> GateActivation:
    defaults: dict[str, Any] = dict(
        gate="audit_stall",
        action="blocked",
        timestamp="2026-04-26T00:00:00+00:00",
        detail="fired 3x",
    )
    defaults.update(overrides)
    return GateActivation(**defaults)


def _seed_legacy_gate_activations(*records: GateActivation) -> None:
    """Append legacy rows to /fake/data/gate_activations.jsonl (oldest-first)."""
    ledger = JsonlGateActivationLedger(_LEGACY_DIR)
    for record in records:
        ledger.append(record)


def test_state_store_creates_gate_activations_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='gate_activations'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_gate_activations_round_trip() -> None:
    store = StateStore(":memory:")
    a = _gate_activation(gate="g1")
    b = _gate_activation(gate="g2")
    store.append_gate_activation(a)
    store.append_gate_activation(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order, and the whole
    # record round-trips via the JSON blob.
    assert store.load_gate_activations() == [a, b]


def test_load_gate_activations_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_gate_activations() == []


def test_load_gate_activations_hydrates_with_field_filter() -> None:
    """A blob with an unknown key hydrates cleanly (dropped, not raised).

    The SQLite reader shares the JSONL core
    (:func:`_parse_gate_activation_dict`): it drops keys outside
    ``fields(GateActivation)`` — so a blob written by a newer schema (extra
    keys) round-trips through the known fields.
    """
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO gate_activations (data) VALUES (?)",
        (json.dumps({
            "gate": "audit_stall",
            "action": "blocked",
            "timestamp": "2026-04-26T00:00:00+00:00",
            "detail": "fired 3x",
            "unknown_future_field": "ignored",  # dropped
        }),),
    )
    store._conn.commit()
    loaded = store.load_gate_activations()
    assert len(loaded) == 1
    assert loaded[0].gate == "audit_stall"
    assert loaded[0].detail == "fired 3x"


def test_load_gate_activations_drops_structurally_corrupt_blob() -> None:
    """A blob missing a required field is dropped (not raised), matching JSONL."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO gate_activations (data) VALUES (?)",
        (json.dumps({"gate": "audit_stall"}),),  # no action/timestamp/detail
    )
    store._conn.commit()
    assert store.load_gate_activations() == []


def test_load_gate_activations_tail_returns_bounded_oldest_first() -> None:
    """The bounded tail read returns the newest *limit* rows oldest-first."""
    store = StateStore(":memory:")
    for i in range(4):
        store.append_gate_activation(_gate_activation(gate=f"g{i}"))
    tail = store.load_gate_activations_tail(2)
    assert [r.gate for r in tail] == ["g2", "g3"]
    # A limit beyond the trace length returns the whole (short) trace.
    assert [r.gate for r in store.load_gate_activations_tail(99)] == [
        "g0", "g1", "g2", "g3",
    ]


def test_load_gate_activations_tail_nonpositive_limit_returns_empty() -> None:
    """A limit of zero or negative short-circuits without a query."""
    store = StateStore(":memory:")
    store.append_gate_activation(_gate_activation(gate="g0"))
    assert store.load_gate_activations_tail(0) == []
    assert store.load_gate_activations_tail(-1) == []


def test_load_gate_activations_tail_drops_structurally_corrupt_blob() -> None:
    """A field-short blob in the tail window is dropped (not raised)."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO gate_activations (data) VALUES (?)",
        (json.dumps({"gate": "audit_stall"}),),  # no action/timestamp/detail
    )
    store._conn.commit()
    assert store.load_gate_activations_tail(5) == []


def test_gate_activation_adapter_read_tail_delegates(
    mem_fs: dict[str, str],
) -> None:
    """The SQLite adapter's read_tail seeds once, then bounds via the store."""
    _seed_legacy_gate_activations(
        _gate_activation(gate="g1"),
        _gate_activation(gate="g2"),
        _gate_activation(gate="g3"),
    )
    ledger = SqliteGateActivationLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert [r.gate for r in ledger.read_tail(2)] == ["g2", "g3"]


def test_load_gate_activations_tail_for_gate_filters_and_drops_corrupt() -> None:
    """The gate-scoped tail skips other gates and drops a field-short blob."""
    store = StateStore(":memory:")
    store.append_gate_activation(_gate_activation(gate="a", detail="1"))
    store.append_gate_activation(_gate_activation(gate="b", detail="x"))
    store.append_gate_activation(_gate_activation(gate="a", detail="2"))
    store._conn.execute(
        "INSERT INTO gate_activations (data) VALUES (?)",
        (json.dumps({"gate": "a"}),),  # no action/timestamp/detail
    )
    store._conn.commit()
    tail = store.load_gate_activations_tail_for_gate("a", 5)
    assert [r.detail for r in tail] == ["1", "2"]
    assert store.load_gate_activations_tail_for_gate("a", -1) == []


def test_gate_activation_adapter_read_gate_tail_seeds_then_filters(
    mem_fs: dict[str, str],
) -> None:
    """The SQLite adapter's read_gate_tail seeds once, then filters via the store."""
    _seed_legacy_gate_activations(
        _gate_activation(gate="a", detail="1"),
        _gate_activation(gate="b", detail="x"),
        _gate_activation(gate="a", detail="2"),
    )
    ledger = SqliteGateActivationLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert [r.detail for r in ledger.read_gate_tail("a", 1)] == ["2"]


def test_load_recent_gate_activations_for_gate_filters_and_bounds(
    mem_fs: dict[str, str],
) -> None:
    """Only *gate*'s newest *limit* JSONL rows return oldest-first (#4071)."""
    from maestro.audit_checks import load_recent_gate_activations_for_gate

    def row(gate: str, detail: str) -> str:
        return json.dumps(
            {"gate": gate, "action": "blocked", "timestamp": "t", "detail": detail},
        )

    lines = [
        row("a", "1"),
        row("a", "2"),
        row("b", "x"),
        "",
        '{"gate": "a" not-json',  # carries the gate substring → JSONDecodeError
        '["a"]',  # non-dict row that passes the substring pre-filter
        json.dumps({"gate": "a"}),  # field-short → parser None, skipped
        row("a", "3"),
        row("b", "y"),
    ]
    mem_fs[str(_LEGACY_DIR / "gate_activations.jsonl")] = "\n".join(lines) + "\n"
    tail = load_recent_gate_activations_for_gate(_LEGACY_DIR, "a", 2)
    assert [r.detail for r in tail] == ["2", "3"]
    everything = load_recent_gate_activations_for_gate(_LEGACY_DIR, "a", 99)
    assert [r.detail for r in everything] == ["1", "2", "3"]
    assert load_recent_gate_activations_for_gate(_LEGACY_DIR, "a", 0) == []
    assert load_recent_gate_activations_for_gate(_LEGACY_DIR, "zzz", 5) == []


def test_load_recent_gate_activations_for_gate_missing_or_unreadable(
    mem_fs: dict[str, str],
) -> None:
    """A missing file or an OSError yields an empty gate-scoped tail."""
    from maestro.audit_checks import load_recent_gate_activations_for_gate

    assert load_recent_gate_activations_for_gate(_LEGACY_DIR, "a", 5) == []
    mem_fs[str(_LEGACY_DIR / "gate_activations.jsonl")] = "x"
    with patch.object(Path, "read_text", side_effect=OSError("disk error")):
        assert load_recent_gate_activations_for_gate(_LEGACY_DIR, "a", 5) == []


def test_gate_activation_read_gate_tail_jsonl_vs_sqlite_parity(
    mem_fs: dict[str, str],
) -> None:
    """The gate-scoped tail read is identical on both substrates (#4071)."""
    corpus = [
        _gate_activation(gate="a" if i % 2 else "b", detail=str(i))
        for i in range(7)
    ]
    jsonl = JsonlGateActivationLedger(Path("/fake/gate-tail-parity"))
    sqlite = SqliteGateActivationLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.append(record)
    assert jsonl.read_gate_tail("a", 2) == sqlite.read_gate_tail("a", 2)
    assert [r.detail for r in sqlite.read_gate_tail("a", 2)] == ["3", "5"]


def test_seed_gate_activations_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _gate_activation(gate="g1"),
        _gate_activation(gate="g2"),
        _gate_activation(gate="g3"),
    ]
    store.seed_gate_activations_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id ASC is
    # oldest-first — matching the JSONL forward walk.
    assert [r.gate for r in store.load_gate_activations()] == ["g1", "g2", "g3"]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_gate_activations_seeded() is True


def test_seed_gate_activations_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_gate_activations_once([])
    assert store.load_gate_activations() == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_gate_activations_seeded() is True


def test_seed_gate_activations_once_idempotent_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [_gate_activation(gate="g1"), _gate_activation(gate="g2")]
    store.seed_gate_activations_once(legacy)
    store.seed_gate_activations_once(legacy)
    assert len(store.load_gate_activations()) == 2


def test_seed_gate_activations_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_gate_activations_once([])  # empty seed stamps the durable marker
    store.seed_gate_activations_once([_gate_activation(gate="g9")])
    assert store.load_gate_activations() == []


def test_seed_gate_activations_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_gate_activation(_gate_activation(gate="g1"))
    store.seed_gate_activations_once([_gate_activation(gate="g2")])
    loaded = store.load_gate_activations()
    assert len(loaded) == 1
    assert loaded[0].gate == "g1"
    assert store.is_gate_activations_seeded() is True


def test_gate_activations_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_gate_activations_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_gate_activations_seeded() is False
    store.seed_gate_activations_once([])
    assert store.is_gate_activations_seeded() is True


def test_gate_activation_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_gate_activations(
        _gate_activation(gate="g1"),
        _gate_activation(gate="g2"),
    )
    ledger = SqliteGateActivationLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    # Oldest-first, and the full history carried over.
    assert [r.gate for r in ledger.read_all()] == ["g1", "g2"]


def test_gate_activation_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_gate_activations(_gate_activation(gate="g1"))
    store = StateStore(":memory:")
    first = SqliteGateActivationLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.read_all()) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteGateActivationLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.read_all()) == 1


def test_gate_activation_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_gate_activations(_gate_activation(gate="g1"))
    ledger = SqliteGateActivationLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.read_all()) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.read_all()) == 1


def test_gate_activation_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_gate_activations(_gate_activation(gate="g1"))
    store = StateStore(":memory:")
    store.seed_gate_activations_once([])  # empty seed stamps the durable marker
    ledger = SqliteGateActivationLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.read_all() == []  # marker blocks the re-seed
    assert store.is_gate_activations_seeded() is True


def test_gate_activation_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteGateActivationLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.read_all() == []


def test_gate_activation_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteGateActivationLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.read_all() == []


def test_gate_activation_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / GATE_ACTIVATIONS_FILENAME)] = ""
    ledger = SqliteGateActivationLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.read_all() == []


def test_gate_activation_adapter_append_persists() -> None:
    ledger = SqliteGateActivationLedger(StateStore(":memory:"))
    ledger.append(_gate_activation(gate="g7"))
    loaded = ledger.read_all()
    assert len(loaded) == 1
    assert loaded[0].gate == "g7"


def test_gate_activation_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """JSONL and SQLite adapters return identical ``GateActivation`` lists.

    A shared corpus round-trips identically through both substrates, so flipping
    ``[paths] sqlite_state_enabled`` never changes what the gate-activation
    readers see — the core proposal-106 parity guarantee.
    """
    corpus = [
        _gate_activation(gate="g1", action="blocked", detail="d1"),
        _gate_activation(gate="g2", action="directive_set", detail="d2"),
    ]
    jsonl = JsonlGateActivationLedger(Path("/fake/parity-ga"))
    sqlite = SqliteGateActivationLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.append(record)
    assert jsonl.read_all() == corpus
    assert sqlite.read_all() == corpus
    assert jsonl.read_all() == sqlite.read_all()


# ---------------------------------------------------------------------------
# conscious_episodes table + seed-once + SqliteConsciousEpisodeLedger
# ---------------------------------------------------------------------------


def _ce_episode(
    moment_id: str = "m1", item_id: str = "i1",
    receipts: tuple[BroadcastReceipt, ...] = (),
) -> ConsciousEpisode:
    item = build_workspace_item(
        item_id=item_id, source_kind="audit_finding", source_ref="ref",
        observed_at="2026-04-26T00:00:00+00:00", trust="mechanical_observation",
        confidence=0.5, expires_at=None, summary="a bounded summary",
    )
    schema = AttentionSchema(
        focus="focus", uncertainty=0.2, capacity_used=1, omitted_count=0,
        permission_summary="all", blind_spots=("gap",),
    )
    moment = ConsciousMoment(
        moment_id=moment_id, predecessor_id=None, continuity="genesis",
        assembled_at="2026-04-26T00:00:00+00:00", policy_version="v1",
        items=(item,), excluded=(), schema=schema,
        candidate_hash="ch", selection_hash="sh",
        max_items=MAX_MOMENT_ITEMS, max_chars=MAX_MOMENT_CHARS,
    )
    return ConsciousEpisode(
        moment=moment, receipts=receipts, cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-04-26T00:00:00+00:00",
    )


def _seed_legacy_conscious_episodes(*episodes: ConsciousEpisode) -> None:
    """Append legacy rows to /fake/data/conscious_episodes.jsonl (oldest-first)."""
    ledger = JsonlConsciousEpisodeLedger(_LEGACY_DIR)
    for episode in episodes:
        ledger.append(episode)


def test_state_store_creates_conscious_episodes_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='conscious_episodes'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_conscious_episodes_round_trip() -> None:
    store = StateStore(":memory:")
    a = _ce_episode(moment_id="m1")
    b = _ce_episode(moment_id="m2")
    store.append_conscious_episode(a)
    store.append_conscious_episode(b)
    # Oldest-first (ORDER BY id ASC); the whole nested record round-trips via the
    # single JSON blob and the shared tuple-coercing parser.
    assert store.load_conscious_episodes() == [a, b]


def test_append_and_load_populated_receipts_round_trip() -> None:
    """A populated ``receipts`` tuple survives the SQLite append→load round-trip
    (proposal 130 persists receipts; the round-trip above covered only the empty
    case)."""
    store = StateStore(":memory:")
    receipt = BroadcastReceipt(
        moment_id="m1", moment_hash="sh", consumer="contemplation",
        delivered_at="2026-04-26T00:00:01+00:00", redacted_item_ids=("i9",),
    )
    episode = _ce_episode(moment_id="m1", receipts=(receipt,))
    store.append_conscious_episode(episode)
    loaded = store.load_conscious_episodes()
    assert loaded == [episode]
    assert loaded[0].receipts == (receipt,)


def test_load_conscious_episodes_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_conscious_episodes() == []


def test_load_conscious_episodes_hydrates_with_extra_key() -> None:
    """A blob with an unknown key hydrates cleanly (dropped, not raised)."""
    store = StateStore(":memory:")
    blob = {**episode_to_dict(_ce_episode()), "unknown_future_field": "ignored"}
    store._conn.execute(
        "INSERT INTO conscious_episodes (data) VALUES (?)",
        (json.dumps(blob),),
    )
    store._conn.commit()
    loaded = store.load_conscious_episodes()
    assert len(loaded) == 1
    assert loaded[0].moment.moment_id == "m1"


def test_load_conscious_episodes_drops_structurally_corrupt_blob() -> None:
    """A blob that is a dict but not an episode is dropped (not raised)."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO conscious_episodes (data) VALUES (?)",
        (json.dumps({"not": "an episode"}),),
    )
    store._conn.commit()
    assert store.load_conscious_episodes() == []


def test_load_conscious_episodes_tail_returns_bounded_oldest_first() -> None:
    """The bounded tail read returns the newest *limit* rows oldest-first."""
    store = StateStore(":memory:")
    for i in range(4):
        store.append_conscious_episode(_ce_episode(moment_id=f"m{i}"))
    tail = store.load_conscious_episodes_tail(2)
    assert [e.moment.moment_id for e in tail] == ["m2", "m3"]
    assert [e.moment.moment_id for e in store.load_conscious_episodes_tail(99)] == [
        "m0", "m1", "m2", "m3",
    ]


def test_load_conscious_episodes_tail_nonpositive_limit_returns_empty() -> None:
    """A limit of zero or negative short-circuits without a query."""
    store = StateStore(":memory:")
    store.append_conscious_episode(_ce_episode(moment_id="m0"))
    assert store.load_conscious_episodes_tail(0) == []
    assert store.load_conscious_episodes_tail(-1) == []


def test_load_conscious_episodes_tail_drops_structurally_corrupt_blob() -> None:
    """A structurally-corrupt blob in the tail window is dropped (not raised)."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO conscious_episodes (data) VALUES (?)",
        (json.dumps({"not": "an episode"}),),
    )
    store._conn.commit()
    assert store.load_conscious_episodes_tail(5) == []


def test_seed_conscious_episodes_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _ce_episode(moment_id="m1"),
        _ce_episode(moment_id="m2"),
        _ce_episode(moment_id="m3"),
    ]
    store.seed_conscious_episodes_once(oldest_first)
    assert [e.moment.moment_id for e in store.load_conscious_episodes()] == [
        "m1", "m2", "m3",
    ]
    assert store.is_conscious_episodes_seeded() is True


def test_seed_conscious_episodes_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_conscious_episodes_once([])
    assert store.load_conscious_episodes() == []
    assert store.is_conscious_episodes_seeded() is True


def test_seed_conscious_episodes_once_idempotent_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [_ce_episode(moment_id="m1"), _ce_episode(moment_id="m2")]
    store.seed_conscious_episodes_once(legacy)
    store.seed_conscious_episodes_once(legacy)
    assert len(store.load_conscious_episodes()) == 2


def test_seed_conscious_episodes_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_conscious_episodes_once([])  # empty seed stamps the durable marker
    store.seed_conscious_episodes_once([_ce_episode(moment_id="m9")])
    assert store.load_conscious_episodes() == []


def test_seed_conscious_episodes_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted."""
    store = StateStore(":memory:")
    store.append_conscious_episode(_ce_episode(moment_id="m1"))
    store.seed_conscious_episodes_once([_ce_episode(moment_id="m2")])
    loaded = store.load_conscious_episodes()
    assert len(loaded) == 1
    assert loaded[0].moment.moment_id == "m1"
    assert store.is_conscious_episodes_seeded() is True


def test_conscious_episodes_seed_marker_defaults_false_then_stamps() -> None:
    store = StateStore(":memory:")
    assert store.is_conscious_episodes_seeded() is False
    store.seed_conscious_episodes_once([])
    assert store.is_conscious_episodes_seeded() is True


def test_conscious_episode_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_conscious_episodes(
        _ce_episode(moment_id="m1"),
        _ce_episode(moment_id="m2"),
    )
    ledger = SqliteConsciousEpisodeLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert [e.moment.moment_id for e in ledger.read_all()] == ["m1", "m2"]


def test_conscious_episode_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_conscious_episodes(_ce_episode(moment_id="m1"))
    store = StateStore(":memory:")
    first = SqliteConsciousEpisodeLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.read_all()) == 1
    second = SqliteConsciousEpisodeLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.read_all()) == 1


def test_conscious_episode_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_conscious_episodes(_ce_episode(moment_id="m1"))
    ledger = SqliteConsciousEpisodeLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.read_all()) == 1  # seeds and flips the in-memory guard
    assert len(ledger.read_all()) == 1  # second access must not re-check


def test_conscious_episode_adapter_durable_marker_blocks_reseed_over_empty(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_conscious_episodes(_ce_episode(moment_id="m1"))
    store = StateStore(":memory:")
    store.seed_conscious_episodes_once([])  # empty seed stamps the durable marker
    ledger = SqliteConsciousEpisodeLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.read_all() == []
    assert store.is_conscious_episodes_seeded() is True


def test_conscious_episode_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteConsciousEpisodeLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.read_all() == []


def test_conscious_episode_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteConsciousEpisodeLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.read_all() == []


def test_conscious_episode_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / CONSCIOUS_EPISODES_FILENAME)] = ""
    ledger = SqliteConsciousEpisodeLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.read_all() == []


def test_conscious_episode_adapter_append_persists() -> None:
    ledger = SqliteConsciousEpisodeLedger(StateStore(":memory:"))
    ledger.append(_ce_episode(moment_id="m7"))
    loaded = ledger.read_all()
    assert len(loaded) == 1
    assert loaded[0].moment.moment_id == "m7"


def test_conscious_episode_adapter_read_recent_newest_first() -> None:
    """The adapter's read_recent returns newest-first (matching the JSONL side)."""
    ledger = SqliteConsciousEpisodeLedger(StateStore(":memory:"))
    ledger.append(_ce_episode(moment_id="m1"))
    ledger.append(_ce_episode(moment_id="m2"))
    assert [e.moment.moment_id for e in ledger.read_recent(5)] == ["m2", "m1"]


def test_conscious_episode_adapter_is_a_backend() -> None:
    ledger = SqliteConsciousEpisodeLedger(StateStore(":memory:"))
    assert isinstance(ledger, ConsciousEpisodeBackend)


def test_conscious_episode_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """JSONL and SQLite adapters hydrate identical episode lists.

    A shared corpus round-trips identically through both substrates, so flipping
    ``[paths] sqlite_state_enabled`` never changes what the readers see — the core
    proposal-106 parity guarantee, here over the nested tuple-carrying record.
    """
    corpus = [
        _ce_episode(moment_id="m1", item_id="i1"),
        _ce_episode(moment_id="m2", item_id="i2"),
    ]
    jsonl = JsonlConsciousEpisodeLedger(Path("/fake/parity-ce"))
    sqlite = SqliteConsciousEpisodeLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for episode in corpus:
            backend.append(episode)
    assert jsonl.read_all() == corpus
    assert sqlite.read_all() == corpus
    assert jsonl.read_all() == sqlite.read_all()


# ---------------------------------------------------------------------------
# premise_observations table + seed-once + SqlitePremiseLedger (issue #2304)
# ---------------------------------------------------------------------------


def _premise(**overrides: Any) -> PremiseObservation:
    defaults: dict[str, Any] = dict(
        task_id="task-1",
        referenced=2,
        missing=1,
        timestamp="2026-04-26T00:00:00+00:00",
    )
    defaults.update(overrides)
    return PremiseObservation(**defaults)


def _seed_legacy_premise_observations(*records: PremiseObservation) -> None:
    """Append legacy rows to /fake/data/premise_observations.jsonl (oldest-first)."""
    ledger = JsonlPremiseLedger(_LEGACY_DIR)
    for record in records:
        ledger.append(record)


def test_state_store_creates_premise_observations_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='premise_observations'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_premise_observations_round_trip() -> None:
    store = StateStore(":memory:")
    a = _premise(task_id="a")
    b = _premise(task_id="b")
    store.append_premise_observation(a)
    store.append_premise_observation(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order.
    assert store.load_premise_observations() == [a, b]


def test_load_premise_observations_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_premise_observations() == []


def test_load_premise_observations_hydrates_with_field_filter() -> None:
    """A blob with an unknown key hydrates cleanly (dropped, not raised)."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO premise_observations (data) VALUES (?)",
        (json.dumps({
            "task_id": "a",
            "referenced": 3,
            "missing": 0,
            "timestamp": "2026-04-26T00:00:00+00:00",
            "unknown_future_field": "ignored",  # dropped
        }),),
    )
    store._conn.commit()
    loaded = store.load_premise_observations()
    assert len(loaded) == 1
    assert loaded[0].task_id == "a"
    assert loaded[0].referenced == 3


def test_load_premise_observations_drops_structurally_corrupt_blob() -> None:
    """A blob missing a required field is dropped (not raised), matching JSONL."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO premise_observations (data) VALUES (?)",
        (json.dumps({"task_id": "a"}),),  # no referenced/missing/timestamp
    )
    store._conn.commit()
    assert store.load_premise_observations() == []


def test_seed_premise_observations_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [_premise(task_id="a"), _premise(task_id="b")]
    store.seed_premise_observations_once(oldest_first)
    assert [r.task_id for r in store.load_premise_observations()] == ["a", "b"]
    assert store.is_premise_observations_seeded() is True


def test_seed_premise_observations_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_premise_observations_once([])
    assert store.load_premise_observations() == []
    assert store.is_premise_observations_seeded() is True


def test_seed_premise_observations_once_idempotent_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = [_premise(task_id="a"), _premise(task_id="b")]
    store.seed_premise_observations_once(legacy)
    store.seed_premise_observations_once(legacy)
    assert len(store.load_premise_observations()) == 2


def test_seed_premise_observations_once_does_not_double_populated_unmarked() -> (
    None
):
    """A crash-populated-but-unmarked store is stamped, not re-inserted."""
    store = StateStore(":memory:")
    store.append_premise_observation(_premise(task_id="a"))
    store.seed_premise_observations_once([_premise(task_id="b")])
    loaded = store.load_premise_observations()
    assert len(loaded) == 1
    assert loaded[0].task_id == "a"
    assert store.is_premise_observations_seeded() is True


def test_premise_seed_marker_defaults_false_then_stamps() -> None:
    store = StateStore(":memory:")
    assert store.is_premise_observations_seeded() is False
    store.seed_premise_observations_once([])
    assert store.is_premise_observations_seeded() is True


def test_premise_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_premise_observations(
        _premise(task_id="a"), _premise(task_id="b"),
    )
    ledger = SqlitePremiseLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert [r.task_id for r in ledger.read_all()] == ["a", "b"]


def test_premise_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_premise_observations(_premise(task_id="a"))
    store = StateStore(":memory:")
    first = SqlitePremiseLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.read_all()) == 1
    second = SqlitePremiseLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.read_all()) == 1


def test_premise_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_premise_observations(_premise(task_id="a"))
    ledger = SqlitePremiseLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.read_all()) == 1  # seeds and flips the in-memory guard
    assert len(ledger.read_all()) == 1  # second access does not re-seed


def test_premise_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_premise_observations(_premise(task_id="a"))
    store = StateStore(":memory:")
    store.seed_premise_observations_once([])  # empty seed stamps the marker
    ledger = SqlitePremiseLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.read_all() == []  # marker blocks the re-seed
    assert store.is_premise_observations_seeded() is True


def test_premise_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqlitePremiseLedger(StateStore(":memory:"), legacy_jsonl_dir=None)
    assert ledger.read_all() == []


def test_premise_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqlitePremiseLedger(
        StateStore(":memory:"), legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.read_all() == []


def test_premise_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / PREMISE_OBSERVATIONS_FILENAME)] = ""
    ledger = SqlitePremiseLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.read_all() == []


def test_premise_adapter_append_persists() -> None:
    ledger = SqlitePremiseLedger(StateStore(":memory:"))
    ledger.append(_premise(task_id="z"))
    loaded = ledger.read_all()
    assert len(loaded) == 1
    assert loaded[0].task_id == "z"


# ---------------------------------------------------------------------------
# predictions table + seed-once + SqlitePredictionLedger
# ---------------------------------------------------------------------------


def _prediction(**overrides: Any) -> dict[str, object]:
    """Build a task-outcome prediction row (the simplest of the three shapes)."""
    defaults: dict[str, object] = dict(
        task_id="t1",
        predicted_outcome="success",
        timestamp="2026-04-26T00:00:00+00:00",
    )
    defaults.update(overrides)
    return defaults


def _seed_legacy_predictions(*records: dict[str, object]) -> None:
    """Append legacy rows to /fake/data/predictions.jsonl (oldest-first)."""
    ledger = JsonlPredictionLedger(_LEGACY_DIR)
    for record in records:
        ledger.append(record)


def test_state_store_creates_predictions_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='predictions'",
    )
    assert cur.fetchone() is not None


def test_append_and_load_predictions_round_trip() -> None:
    store = StateStore(":memory:")
    a = _prediction(task_id="t1")
    b = _prediction(task_id="t2")
    store.append_prediction(a)
    store.append_prediction(b)
    # Oldest-first (ORDER BY id ASC) preserves the append order, and each row's
    # heterogeneous dict round-trips verbatim via the JSON blob.
    assert store.load_predictions() == [a, b]


def test_append_and_load_predictions_preserves_three_shapes() -> None:
    """All three heterogeneous prediction row shapes round-trip verbatim."""
    store = StateStore(":memory:")
    task_row = _prediction(task_id="t1")
    reflection_row: dict[str, object] = {
        "source": "reflection",
        "prediction_id": "reflection-3",
        "prediction": "I will land PR #9",
        "loss_condition": "PR #9 stays open",
        "verdict": "unscored",
        "timestamp": "2026-04-26T00:01:00+00:00",
    }
    verdict_row: dict[str, object] = {
        "source": "reflection_verdict",
        "prediction_id": "reflection-3",
        "verdict": "correct",
        "timestamp": "2026-04-26T00:02:00+00:00",
    }
    for row in (task_row, reflection_row, verdict_row):
        store.append_prediction(row)
    assert store.load_predictions() == [task_row, reflection_row, verdict_row]


def test_load_predictions_empty_store() -> None:
    store = StateStore(":memory:")
    assert store.load_predictions() == []


def test_load_predictions_drops_structurally_corrupt_blob() -> None:
    """A blob that does not decode to a JSON object is dropped (not raised)."""
    store = StateStore(":memory:")
    store._conn.execute(
        "INSERT INTO predictions (data) VALUES (?)",
        (json.dumps([1, 2, 3]),),  # a JSON list, not an object
    )
    store._conn.commit()
    assert store.load_predictions() == []


def test_load_predictions_keeps_extra_fields_verbatim() -> None:
    """A prediction blob keeps unknown keys — no dataclass field-filter.

    The three shapes are stored verbatim as JSON objects, so a blob written by a
    newer schema round-trips its extra keys untouched (additive fields never
    force a migration).
    """
    store = StateStore(":memory:")
    row = {
        "task_id": "t1",
        "predicted_outcome": "success",
        "timestamp": "2026-04-26T00:00:00+00:00",
        "future_field": "kept",
    }
    store._conn.execute(
        "INSERT INTO predictions (data) VALUES (?)",
        (json.dumps(row),),
    )
    store._conn.commit()
    assert store.load_predictions() == [row]


def test_seed_predictions_once_preserves_order() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _prediction(task_id="t1"),
        _prediction(task_id="t2"),
        _prediction(task_id="t3"),
    ]
    store.seed_predictions_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id ASC is
    # oldest-first — matching the JSONL forward walk.
    assert [r["task_id"] for r in store.load_predictions()] == ["t1", "t2", "t3"]
    assert store.is_predictions_seeded() is True


def test_seed_predictions_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_predictions_once([])
    assert store.load_predictions() == []
    assert store.is_predictions_seeded() is True


def test_seed_predictions_once_idempotent_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = [_prediction(task_id="t1"), _prediction(task_id="t2")]
    store.seed_predictions_once(legacy)
    store.seed_predictions_once(legacy)
    assert len(store.load_predictions()) == 2


def test_seed_predictions_once_skips_when_already_marked() -> None:
    store = StateStore(":memory:")
    store.seed_predictions_once([])  # empty seed stamps the durable marker
    store.seed_predictions_once([_prediction(task_id="t9")])
    assert store.load_predictions() == []


def test_seed_predictions_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted."""
    store = StateStore(":memory:")
    store.append_prediction(_prediction(task_id="t1"))
    store.seed_predictions_once([_prediction(task_id="t2")])
    loaded = store.load_predictions()
    assert len(loaded) == 1
    assert loaded[0]["task_id"] == "t1"
    assert store.is_predictions_seeded() is True


def test_predictions_seed_marker_defaults_false_then_stamps() -> None:
    store = StateStore(":memory:")
    assert store.is_predictions_seeded() is False
    store.seed_predictions_once([])
    assert store.is_predictions_seeded() is True


def test_prediction_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_predictions(
        _prediction(task_id="t1"),
        _prediction(task_id="t2"),
    )
    ledger = SqlitePredictionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert [r["task_id"] for r in ledger.read_all()] == ["t1", "t2"]


def test_prediction_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_predictions(_prediction(task_id="t1"))
    store = StateStore(":memory:")
    first = SqlitePredictionLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(first.read_all()) == 1
    second = SqlitePredictionLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert len(second.read_all()) == 1


def test_prediction_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_predictions(_prediction(task_id="t1"))
    ledger = SqlitePredictionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert len(ledger.read_all()) == 1  # seeds and flips the in-memory guard
    assert len(ledger.read_all()) == 1


def test_prediction_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_predictions(_prediction(task_id="t1"))
    store = StateStore(":memory:")
    store.seed_predictions_once([])  # empty seed stamps the durable marker
    ledger = SqlitePredictionLedger(store, legacy_jsonl_dir=_LEGACY_DIR)
    assert ledger.read_all() == []  # marker blocks the re-seed
    assert store.is_predictions_seeded() is True


def test_prediction_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqlitePredictionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.read_all() == []


def test_prediction_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqlitePredictionLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/data/missing"),
    )
    assert ledger.read_all() == []


def test_prediction_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_DIR / PREDICTIONS_FILENAME)] = ""
    ledger = SqlitePredictionLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DIR,
    )
    assert ledger.read_all() == []


def test_prediction_adapter_append_persists() -> None:
    ledger = SqlitePredictionLedger(StateStore(":memory:"))
    ledger.append(_prediction(task_id="t7"))
    loaded = ledger.read_all()
    assert len(loaded) == 1
    assert loaded[0]["task_id"] == "t7"


def test_prediction_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """JSONL and SQLite adapters return identical prediction-row lists.

    A shared corpus of all three shapes round-trips identically through both
    substrates, so flipping ``[paths] sqlite_state_enabled`` never changes what
    the prediction readers see — the core proposal-106 parity guarantee.
    """
    corpus: list[dict[str, object]] = [
        _prediction(task_id="t1", predicted_outcome="success"),
        {
            "source": "reflection",
            "prediction_id": "reflection-1",
            "prediction": "p",
            "loss_condition": "l",
            "verdict": "unscored",
            "timestamp": "2026-04-26T00:01:00+00:00",
        },
        {
            "source": "reflection_verdict",
            "prediction_id": "reflection-1",
            "verdict": "correct",
            "timestamp": "2026-04-26T00:02:00+00:00",
        },
    ]
    jsonl = JsonlPredictionLedger(Path("/fake/parity-pred"))
    sqlite = SqlitePredictionLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.append(record)
    assert jsonl.read_all() == corpus
    assert sqlite.read_all() == corpus
    assert jsonl.read_all() == sqlite.read_all()


# ---------------------------------------------------------------------------
# SqliteDeedLedger adapter — record / count / recent
# ---------------------------------------------------------------------------


def test_deed_adapter_record_returns_and_persists() -> None:
    ledger = SqliteDeedLedger(StateStore(":memory:"))
    deed = ledger.record(
        DeedType.PR_CLOSE, 42, "closed", now="2026-07-31T00:00:00+00:00",
    )
    assert deed.deed_type == "pr_close"
    assert deed.pr_number == 42
    assert deed.timestamp == "2026-07-31T00:00:00+00:00"
    assert ledger.recent(10) == [deed]
    assert ledger.count() == 1


def test_deed_adapter_record_accepts_raw_string_type() -> None:
    ledger = SqliteDeedLedger(StateStore(":memory:"))
    deed = ledger.record("merge", 5, "merged", now="2026-07-31T00:00:00+00:00")
    assert deed.deed_type == "merge"


def test_deed_adapter_record_default_now_stamps_utc() -> None:
    ledger = SqliteDeedLedger(StateStore(":memory:"))
    deed = ledger.record(DeedType.MERGE, 1, "d")
    assert deed.timestamp  # non-empty ISO string (current UTC)
    assert ledger.count() == 1


def test_deed_adapter_count_windows_by_since() -> None:
    ledger = SqliteDeedLedger(StateStore(":memory:"))
    ledger.record(DeedType.MERGE, 1, "old", now="2026-07-30T00:00:00+00:00")
    ledger.record(DeedType.MERGE, 2, "new", now="2026-08-01T00:00:00+00:00")
    assert ledger.count(since="2026-07-31T00:00:00+00:00") == 1
    assert ledger.count() == 2


# ---------------------------------------------------------------------------
# StateStore — owned-backlog snapshot table
# ---------------------------------------------------------------------------


def _bsnap(iteration: int, count: int, ts: str) -> BacklogSnapshot:
    """Build a fully-specified snapshot for deterministic equality."""
    return BacklogSnapshot(iteration=iteration, count=count, timestamp=ts)


def test_state_store_creates_backlog_snapshots_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='backlog_snapshots'",
    )
    assert cur.fetchone() is not None


def test_append_and_recent_backlog_snapshots_round_trip() -> None:
    store = StateStore(":memory:")
    a = _bsnap(1, 40, "2026-01-01T00:00:01+00:00")
    b = _bsnap(2, 37, "2026-01-01T00:00:02+00:00")
    store.append_backlog_snapshot(a)
    store.append_backlog_snapshot(b)
    # Newest-first (ORDER BY id DESC).
    assert store.recent_backlog_snapshots(10) == [b, a]


def test_recent_backlog_snapshots_honours_limit_newest_first() -> None:
    store = StateStore(":memory:")
    for i in range(5):
        store.append_backlog_snapshot(
            _bsnap(i, i * 10, f"2026-01-01T00:00:0{i}+00:00"),
        )
    got = store.recent_backlog_snapshots(2)
    assert [s.iteration for s in got] == [4, 3]


def test_recent_backlog_snapshots_non_positive_limit_returns_empty() -> None:
    store = StateStore(":memory:")
    store.append_backlog_snapshot(_bsnap(1, 5, "2026-01-01T00:00:01+00:00"))
    # SQLite treats a negative LIMIT as unbounded — the guard is load-bearing.
    assert store.recent_backlog_snapshots(0) == []
    assert store.recent_backlog_snapshots(-1) == []


def test_seed_backlog_snapshots_once_bulk_insert_preserves_chronology() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _bsnap(1, 40, "2026-01-01T00:00:01+00:00"),
        _bsnap(2, 37, "2026-01-01T00:00:02+00:00"),
        _bsnap(3, 41, "2026-01-01T00:00:03+00:00"),
    ]
    store.seed_backlog_snapshots_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id DESC is
    # newest-first — matching the JSONL reverse read.
    assert [s.iteration for s in store.recent_backlog_snapshots(10)] == [3, 2, 1]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_owned_backlog_seeded() is True


def test_seed_backlog_snapshots_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_backlog_snapshots_once([])
    assert store.recent_backlog_snapshots(10) == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_owned_backlog_seeded() is True


def test_seed_backlog_snapshots_once_is_idempotent_and_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops.

    Regression for the append-only doubling race (proposal 106): the guarded
    seed re-reads the durable marker inside its ``BEGIN IMMEDIATE`` critical
    section, so a repeat call — the single-connection analogue of a second
    process racing the first seed — inserts nothing.
    """
    store = StateStore(":memory:")
    legacy = [
        _bsnap(1, 10, "2026-01-01T00:00:01+00:00"),
        _bsnap(2, 12, "2026-01-01T00:00:02+00:00"),
    ]
    store.seed_backlog_snapshots_once(legacy)
    store.seed_backlog_snapshots_once(legacy)
    assert len(store.recent_backlog_snapshots(10)) == 2


def test_seed_backlog_snapshots_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_backlog_snapshots_once([])  # empty seed stamps the marker
    store.seed_backlog_snapshots_once(
        [_bsnap(1, 10, "2026-01-01T00:00:01+00:00")],
    )
    assert store.recent_backlog_snapshots(10) == []


def test_seed_backlog_snapshots_once_does_not_double_populated_store() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_backlog_snapshot(_bsnap(9, 99, "2026-01-01T00:00:00+00:00"))
    store.seed_backlog_snapshots_once(
        [_bsnap(1, 10, "2026-01-01T00:00:01+00:00")],
    )
    assert [s.iteration for s in store.recent_backlog_snapshots(10)] == [9]
    assert store.is_owned_backlog_seeded() is True


def test_owned_backlog_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_backlog_snapshots_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_owned_backlog_seeded() is False
    store.seed_backlog_snapshots_once([])
    assert store.is_owned_backlog_seeded() is True


# ---------------------------------------------------------------------------
# SqliteOwnedBacklogLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------

_LEGACY_BACKLOG_DIR = Path("/fake/backlog")


def _seed_legacy_backlog(*snaps: tuple[int, int, str]) -> None:
    """Append legacy snapshots to the legacy JSONL (oldest-first)."""
    for iteration, count, ts in snaps:
        record_owned_backlog_snapshot(
            _LEGACY_BACKLOG_DIR, iteration, count, now=ts,
        )


def test_backlog_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_backlog(
        (1, 40, "2026-01-01T00:00:01+00:00"),
        (2, 37, "2026-01-01T00:00:02+00:00"),
    )
    ledger = SqliteOwnedBacklogLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_BACKLOG_DIR,
    )
    # Newest-first, with the full cycle-over-cycle history carried over.
    assert ledger.recent(10) == [
        _bsnap(2, 37, "2026-01-01T00:00:02+00:00"),
        _bsnap(1, 40, "2026-01-01T00:00:01+00:00"),
    ]


def test_backlog_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_backlog((1, 40, "2026-01-01T00:00:01+00:00"))
    store = StateStore(":memory:")
    first = SqliteOwnedBacklogLedger(
        store, legacy_jsonl_dir=_LEGACY_BACKLOG_DIR,
    )
    assert len(first.recent(10)) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteOwnedBacklogLedger(
        store, legacy_jsonl_dir=_LEGACY_BACKLOG_DIR,
    )
    assert len(second.recent(10)) == 1


def test_backlog_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_backlog((1, 40, "2026-01-01T00:00:01+00:00"))
    ledger = SqliteOwnedBacklogLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_BACKLOG_DIR,
    )
    assert len(ledger.recent(10)) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.recent(10)) == 1


def test_backlog_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present.

    Regression for the durable seed marker: a fresh adapter's in-memory guard is
    reset on restart, so only the persisted marker prevents re-seeding an empty
    store from the still-present legacy JSONL (which would resurrect the history).
    """
    _seed_legacy_backlog((1, 40, "2026-01-01T00:00:01+00:00"))
    store = StateStore(":memory:")
    store.seed_backlog_snapshots_once([])  # empty seed stamps the marker
    ledger = SqliteOwnedBacklogLedger(
        store, legacy_jsonl_dir=_LEGACY_BACKLOG_DIR,
    )
    assert ledger.recent(10) == []  # marker blocks the re-seed
    assert store.is_owned_backlog_seeded() is True


def test_backlog_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteOwnedBacklogLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.recent(10) == []


def test_backlog_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteOwnedBacklogLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/backlog/missing"),
    )
    assert ledger.recent(10) == []


def test_backlog_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_BACKLOG_DIR / _OWNED_BACKLOG_LEDGER_FILE)] = ""
    ledger = SqliteOwnedBacklogLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_BACKLOG_DIR,
    )
    assert ledger.recent(10) == []


def test_backlog_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_backlog((1, 99, "2026-01-01T00:00:01+00:00"))
    store = StateStore(":memory:")
    store.append_backlog_snapshot(_bsnap(5, 3, "2026-01-01T00:00:00+00:00"))
    ledger = SqliteOwnedBacklogLedger(
        store, legacy_jsonl_dir=_LEGACY_BACKLOG_DIR,
    )
    assert [s.iteration for s in ledger.recent(10)] == [5]


# ---------------------------------------------------------------------------
# SqliteOwnedBacklogLedger adapter — record / recent
# ---------------------------------------------------------------------------


def test_backlog_adapter_record_returns_and_persists() -> None:
    ledger = SqliteOwnedBacklogLedger(StateStore(":memory:"))
    snap = ledger.record(7, 4, now="2026-07-31T00:00:00+00:00")
    assert snap == _bsnap(7, 4, "2026-07-31T00:00:00+00:00")
    assert ledger.recent(10) == [snap]


def test_backlog_adapter_record_default_now_stamps_utc() -> None:
    ledger = SqliteOwnedBacklogLedger(StateStore(":memory:"))
    snap = ledger.record(1, 2)
    assert snap.timestamp  # non-empty ISO string (current UTC)
    assert ledger.recent(10) == [snap]


def test_backlog_adapter_recent_non_positive_returns_empty() -> None:
    ledger = SqliteOwnedBacklogLedger(StateStore(":memory:"))
    ledger.record(1, 5, now="2026-07-31T00:00:00+00:00")
    assert ledger.recent(0) == []
    assert ledger.recent(-1) == []


# ---------------------------------------------------------------------------
# PR-admission provenance ledger — table, store methods, seed, adapter
# ---------------------------------------------------------------------------


def _prec(pr_number: int, channel: str = "tend_route") -> ProvenanceRecord:
    """Build a :class:`ProvenanceRecord` for deterministic equality."""
    return ProvenanceRecord(pr_number=pr_number, channel=channel)


def test_state_store_creates_provenance_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='provenance'",
    )
    assert cur.fetchone() is not None


def test_append_and_recent_provenance_round_trip() -> None:
    store = StateStore(":memory:")
    a = _prec(100)
    b = _prec(200)
    store.append_provenance(a)
    store.append_provenance(b)
    # Newest-first (ORDER BY id DESC).
    assert store.recent_provenance(10) == [b, a]


def test_recent_provenance_honours_limit_newest_first() -> None:
    store = StateStore(":memory:")
    for i in range(5):
        store.append_provenance(_prec(i + 1))
    got = store.recent_provenance(2)
    assert [r.pr_number for r in got] == [5, 4]


def test_recent_provenance_non_positive_limit_returns_empty() -> None:
    store = StateStore(":memory:")
    store.append_provenance(_prec(1))
    # SQLite treats a negative LIMIT as unbounded — the guard is load-bearing.
    assert store.recent_provenance(0) == []
    assert store.recent_provenance(-1) == []


def test_seed_provenance_once_bulk_insert_preserves_chronology() -> None:
    store = StateStore(":memory:")
    oldest_first = [_prec(1), _prec(2), _prec(3)]
    store.seed_provenance_once(oldest_first)
    # The AUTOINCREMENT id reproduces append order, so ORDER BY id DESC is
    # newest-first — matching the JSONL trailing-window walk.
    assert [r.pr_number for r in store.recent_provenance(10)] == [3, 2, 1]
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_pr_provenance_seeded() is True


def test_seed_provenance_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_provenance_once([])
    assert store.recent_provenance(10) == []
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_pr_provenance_seeded() is True


def test_seed_provenance_once_is_idempotent_and_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops.

    Regression for the append-only doubling race (proposal 106): the guarded
    seed re-reads the durable marker inside its ``BEGIN IMMEDIATE`` critical
    section, so a repeat call — the single-connection analogue of a second
    process racing the first seed — inserts nothing.
    """
    store = StateStore(":memory:")
    legacy = [_prec(1), _prec(2)]
    store.seed_provenance_once(legacy)
    store.seed_provenance_once(legacy)
    assert len(store.recent_provenance(10)) == 2


def test_seed_provenance_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_provenance_once([])  # empty seed stamps the marker
    store.seed_provenance_once([_prec(1)])
    assert store.recent_provenance(10) == []


def test_seed_provenance_once_does_not_double_populated_store() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_provenance(_prec(9))
    store.seed_provenance_once([_prec(1)])
    assert [r.pr_number for r in store.recent_provenance(10)] == [9]
    assert store.is_pr_provenance_seeded() is True


def test_pr_provenance_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_provenance_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_pr_provenance_seeded() is False
    store.seed_provenance_once([])
    assert store.is_pr_provenance_seeded() is True


# ---------------------------------------------------------------------------
# SqlitePrProvenanceLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------

_LEGACY_PROVENANCE_DIR = Path("/fake/provenance")
_PROVENANCE_FILENAME = "pending_pr_provenance.jsonl"


def _seed_legacy_provenance(*rows: tuple[int, str]) -> None:
    """Append legacy admission rows to the legacy JSONL (oldest-first)."""
    for pr_number, channel in rows:
        log_pending_pr_provenance(_LEGACY_PROVENANCE_DIR, pr_number, channel)


def test_provenance_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_provenance((100, "tend_route"), (200, "tend_route"))
    ledger = SqlitePrProvenanceLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_PROVENANCE_DIR,
    )
    # Newest-first, with the full readmission window carried over.
    assert ledger.recent(10) == [_prec(200), _prec(100)]


def test_provenance_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_provenance((100, "tend_route"))
    store = StateStore(":memory:")
    first = SqlitePrProvenanceLedger(
        store, legacy_jsonl_dir=_LEGACY_PROVENANCE_DIR,
    )
    assert len(first.recent(10)) == 1
    # A second adapter over the already-seeded store must not re-seed.
    second = SqlitePrProvenanceLedger(
        store, legacy_jsonl_dir=_LEGACY_PROVENANCE_DIR,
    )
    assert len(second.recent(10)) == 1


def test_provenance_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_provenance((100, "tend_route"))
    ledger = SqlitePrProvenanceLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_PROVENANCE_DIR,
    )
    assert len(ledger.recent(10)) == 1  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert len(ledger.recent(10)) == 1


def test_provenance_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present.

    Regression for the durable seed marker: a fresh adapter's in-memory guard is
    reset on restart, so only the persisted marker prevents re-seeding an empty
    store from the still-present legacy JSONL (which would double the window).
    """
    _seed_legacy_provenance((100, "tend_route"))
    store = StateStore(":memory:")
    store.seed_provenance_once([])  # empty seed stamps the marker
    ledger = SqlitePrProvenanceLedger(
        store, legacy_jsonl_dir=_LEGACY_PROVENANCE_DIR,
    )
    assert ledger.recent(10) == []  # marker blocks the re-seed
    assert store.is_pr_provenance_seeded() is True


def test_provenance_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqlitePrProvenanceLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.recent(10) == []


def test_provenance_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqlitePrProvenanceLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/provenance/missing"),
    )
    assert ledger.recent(10) == []


def test_provenance_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_PROVENANCE_DIR / _PROVENANCE_FILENAME)] = ""
    ledger = SqlitePrProvenanceLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_PROVENANCE_DIR,
    )
    assert ledger.recent(10) == []


def test_provenance_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_provenance((100, "tend_route"))
    store = StateStore(":memory:")
    store.append_provenance(_prec(5))
    ledger = SqlitePrProvenanceLedger(
        store, legacy_jsonl_dir=_LEGACY_PROVENANCE_DIR,
    )
    assert [r.pr_number for r in ledger.recent(10)] == [5]


# ---------------------------------------------------------------------------
# SqlitePrProvenanceLedger adapter — record / recent
# ---------------------------------------------------------------------------


def test_provenance_adapter_record_persists() -> None:
    ledger = SqlitePrProvenanceLedger(StateStore(":memory:"))
    ledger.record(7, "tend_route")
    assert ledger.recent(10) == [_prec(7)]


def test_provenance_adapter_record_skips_non_positive_pr_number() -> None:
    ledger = SqlitePrProvenanceLedger(StateStore(":memory:"))
    ledger.record(0, "tend_route")
    ledger.record(-3, "tend_route")
    # Mirrors the JSONL writer's guard: phantom admissions are never persisted.
    assert ledger.recent(10) == []


def test_provenance_adapter_recent_non_positive_returns_empty() -> None:
    ledger = SqlitePrProvenanceLedger(StateStore(":memory:"))
    ledger.record(1, "tend_route")
    assert ledger.recent(0) == []
    assert ledger.recent(-1) == []


# ---------------------------------------------------------------------------
# Obligation-fork marker ledger — table, store methods, seed, adapter
# ---------------------------------------------------------------------------


def _ofr(
    task_id: str = "task-1",
    pr_number: int = 100,
    head_sha: str = "head000",
    remote_sha: str = "remote00",
    branch: str = "maestro/pr/branch",
    remote_is_ancestor: bool = False,
) -> ObligationForkRecord:
    """Build an :class:`ObligationForkRecord` for deterministic equality."""
    return ObligationForkRecord(
        task_id=task_id,
        pr_number=pr_number,
        head_sha=head_sha,
        remote_sha=remote_sha,
        branch=branch,
        remote_is_ancestor=remote_is_ancestor,
    )


def _obligation_row_count(store: StateStore) -> int:
    """Return the row count of the obligation-fork table (test-only probe)."""
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM obligation_forks",
        ).fetchone()[0],
    )


def test_state_store_creates_obligation_forks_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='obligation_forks'",
    )
    assert cur.fetchone() is not None


def test_append_and_get_obligation_fork_round_trip() -> None:
    store = StateStore(":memory:")
    store.append_obligation_fork(
        _ofr(
            task_id="t1", pr_number=1, head_sha="h1", remote_sha="r1",
            branch="b1", remote_is_ancestor=True,
        ),
    )
    assert store.get_obligation_fork_for_task("t1") == ObligationFork(
        head_sha="h1", remote_sha="r1", branch="b1", remote_is_ancestor=True,
    )


def test_get_obligation_fork_absent_or_empty_task_returns_none() -> None:
    store = StateStore(":memory:")
    store.append_obligation_fork(_ofr(task_id="t1"))
    assert store.get_obligation_fork_for_task("nope") is None
    # An empty task_id carries no key to match on.
    assert store.get_obligation_fork_for_task("") is None


def test_append_obligation_fork_unique_key_first_wins() -> None:
    """A second row for the same ``(task_id, pr_number)`` is INSERT-OR-IGNOREd.

    Reproduces the JSONL writer's per-``(task_id, pr_number)`` idempotency: the
    first-written SHAs win and the duplicate append is a no-op.
    """
    store = StateStore(":memory:")
    store.append_obligation_fork(
        _ofr(task_id="t1", pr_number=1, head_sha="first"),
    )
    store.append_obligation_fork(
        _ofr(task_id="t1", pr_number=1, head_sha="second"),
    )
    fork = store.get_obligation_fork_for_task("t1")
    assert fork is not None
    assert fork.head_sha == "first"
    assert _obligation_row_count(store) == 1


def test_get_obligation_fork_last_match_wins_for_repeated_task() -> None:
    """Distinct PRs under one task: the highest-id (last) row is returned.

    ``ORDER BY id DESC LIMIT 1`` is the SQLite analogue of the JSONL reader's
    last-matching-row walk.
    """
    store = StateStore(":memory:")
    store.append_obligation_fork(
        _ofr(task_id="t1", pr_number=1, head_sha="old"),
    )
    store.append_obligation_fork(
        _ofr(task_id="t1", pr_number=2, head_sha="new"),
    )
    fork = store.get_obligation_fork_for_task("t1")
    assert fork is not None
    assert fork.head_sha == "new"


def test_seed_obligation_forks_once_preserves_append_order() -> None:
    """Seeding oldest-first makes the later turn win the last-match read.

    The ``AUTOINCREMENT`` id reproduces append order, so a repeated task's most
    recent seeded marker is the one the gate reads.
    """
    store = StateStore(":memory:")
    store.seed_obligation_forks_once(
        [
            _ofr(task_id="t1", pr_number=1, head_sha="old"),
            _ofr(task_id="t1", pr_number=2, head_sha="new"),
        ],
    )
    fork = store.get_obligation_fork_for_task("t1")
    assert fork is not None
    assert fork.head_sha == "new"
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_obligation_fork_seeded() is True


def test_seed_obligation_forks_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_obligation_forks_once([])
    assert store.get_obligation_fork_for_task("task-1") is None
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_obligation_fork_seeded() is True


def test_seed_obligation_forks_once_is_idempotent_and_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops.

    Regression for the append-only doubling race (proposal 106): the guarded
    seed re-reads the durable marker inside its ``BEGIN IMMEDIATE`` critical
    section, so a repeat call inserts nothing.
    """
    store = StateStore(":memory:")
    legacy = [
        _ofr(task_id="t1", pr_number=1),
        _ofr(task_id="t2", pr_number=2),
    ]
    store.seed_obligation_forks_once(legacy)
    store.seed_obligation_forks_once(legacy)
    assert _obligation_row_count(store) == 2


def test_seed_obligation_forks_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_obligation_forks_once([])  # empty seed stamps the marker
    store.seed_obligation_forks_once([_ofr(task_id="t1", pr_number=1)])
    assert store.get_obligation_fork_for_task("t1") is None


def test_seed_obligation_forks_once_does_not_double_populated_store() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted.

    The insert stays guarded on an empty table, so a prior run that inserted
    then crashed before stamping the marker leaves exactly one copy.
    """
    store = StateStore(":memory:")
    store.append_obligation_fork(
        _ofr(task_id="t9", pr_number=9, head_sha="h9"),
    )
    store.seed_obligation_forks_once([_ofr(task_id="t1", pr_number=1)])
    assert store.get_obligation_fork_for_task("t1") is None
    fork = store.get_obligation_fork_for_task("t9")
    assert fork is not None
    assert fork.head_sha == "h9"
    assert store.is_obligation_fork_seeded() is True


def test_obligation_fork_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_obligation_forks_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_obligation_fork_seeded() is False
    store.seed_obligation_forks_once([])
    assert store.is_obligation_fork_seeded() is True


# ---------------------------------------------------------------------------
# SqliteObligationForkLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------

_LEGACY_OBLIGATION_DIR = Path("/fake/obligation")
_OBLIGATION_FILENAME = "obligation_fork_markers.jsonl"


def _seed_legacy_obligation(
    *rows: tuple[str, int, str, str, str, bool],
) -> None:
    """Append legacy obligation-fork rows to the legacy JSONL (oldest-first)."""
    for task_id, pr_number, head_sha, remote_sha, branch, ancestor in rows:
        record_obligation_fork_truth(
            task_id,
            pr_number,
            head_sha,
            remote_sha,
            branch,
            _LEGACY_OBLIGATION_DIR,
            remote_is_ancestor=ancestor,
        )


def test_obligation_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_obligation(
        ("t1", 1, "h1", "r1", "b1", False),
        ("t2", 2, "h2", "r2", "b2", True),
    )
    ledger = SqliteObligationForkLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_OBLIGATION_DIR,
    )
    assert ledger.get("t1") == ObligationFork(
        head_sha="h1", remote_sha="r1", branch="b1", remote_is_ancestor=False,
    )
    assert ledger.get("t2") == ObligationFork(
        head_sha="h2", remote_sha="r2", branch="b2", remote_is_ancestor=True,
    )


def test_obligation_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_obligation(("t1", 1, "h1", "r1", "b1", False))
    store = StateStore(":memory:")
    first = SqliteObligationForkLedger(
        store, legacy_jsonl_dir=_LEGACY_OBLIGATION_DIR,
    )
    assert first.get("t1") is not None
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteObligationForkLedger(
        store, legacy_jsonl_dir=_LEGACY_OBLIGATION_DIR,
    )
    assert second.get("t1") is not None
    assert _obligation_row_count(store) == 1


def test_obligation_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_obligation(("t1", 1, "h1", "r1", "b1", False))
    ledger = SqliteObligationForkLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_OBLIGATION_DIR,
    )
    assert ledger.get("t1") is not None  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert ledger.get("t1") is not None


def test_obligation_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present.

    Only the persisted marker prevents re-seeding an empty store from the
    still-present legacy JSONL after a fresh adapter's in-memory guard resets.
    """
    _seed_legacy_obligation(("t1", 1, "h1", "r1", "b1", False))
    store = StateStore(":memory:")
    store.seed_obligation_forks_once([])  # empty seed stamps the marker
    ledger = SqliteObligationForkLedger(
        store, legacy_jsonl_dir=_LEGACY_OBLIGATION_DIR,
    )
    assert ledger.get("t1") is None  # marker blocks the re-seed
    assert store.is_obligation_fork_seeded() is True


def test_obligation_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteObligationForkLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.get("t1") is None


def test_obligation_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteObligationForkLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/obligation/missing"),
    )
    assert ledger.get("t1") is None


def test_obligation_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_OBLIGATION_DIR / _OBLIGATION_FILENAME)] = ""
    ledger = SqliteObligationForkLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_OBLIGATION_DIR,
    )
    assert ledger.get("t1") is None


def test_obligation_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_obligation(("t1", 1, "h1", "r1", "b1", False))
    store = StateStore(":memory:")
    store.append_obligation_fork(
        _ofr(task_id="t5", pr_number=5, head_sha="h5"),
    )
    ledger = SqliteObligationForkLedger(
        store, legacy_jsonl_dir=_LEGACY_OBLIGATION_DIR,
    )
    # Populated but unmarked → the seed no-ops; t1 never enters, t5 remains.
    assert ledger.get("t1") is None
    fork = ledger.get("t5")
    assert fork is not None
    assert fork.head_sha == "h5"


# ---------------------------------------------------------------------------
# SqliteObligationForkLedger adapter — record / get
# ---------------------------------------------------------------------------


def test_obligation_adapter_record_persists() -> None:
    ledger = SqliteObligationForkLedger(StateStore(":memory:"))
    ledger.record("t1", 1, "h1", "r1", "b1", remote_is_ancestor=True)
    assert ledger.get("t1") == ObligationFork(
        head_sha="h1", remote_sha="r1", branch="b1", remote_is_ancestor=True,
    )


def test_obligation_adapter_record_skips_empty_task_id() -> None:
    ledger = SqliteObligationForkLedger(StateStore(":memory:"))
    ledger.record("", 1, "h1", "r1", "b1")
    # Mirrors the JSONL writer's guard: a keyless marker is never persisted.
    assert ledger.get("") is None
    assert _obligation_row_count(ledger._store) == 0


def test_obligation_adapter_record_honours_unique_key_first_wins() -> None:
    ledger = SqliteObligationForkLedger(StateStore(":memory:"))
    ledger.record("t1", 1, "first", "r1", "b1")
    ledger.record("t1", 1, "second", "r1", "b1")
    fork = ledger.get("t1")
    assert fork is not None
    assert fork.head_sha == "first"
    assert _obligation_row_count(ledger._store) == 1


# ---------------------------------------------------------------------------
# Fetch-truth marker ledger — table, store methods, seed, adapter
# ---------------------------------------------------------------------------


def _ftr(
    task_id: str = "task-1",
    pr_number: int = 100,
    is_ancestor: bool = False,
    head_sha: str = "head000",
) -> FetchTruthRecord:
    """Build a :class:`FetchTruthRecord` for deterministic equality."""
    return FetchTruthRecord(
        task_id=task_id,
        pr_number=pr_number,
        is_ancestor=is_ancestor,
        head_sha=head_sha,
    )


def _fetch_truth_row_count(store: StateStore) -> int:
    """Return the row count of the fetch-truth table (test-only probe)."""
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM fetch_truth_markers",
        ).fetchone()[0],
    )


def test_state_store_creates_fetch_truth_markers_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='fetch_truth_markers'",
    )
    assert cur.fetchone() is not None


def test_append_and_has_fetch_truth_round_trip() -> None:
    store = StateStore(":memory:")
    store.append_fetch_truth(_ftr(task_id="t1", pr_number=1))
    assert store.has_fetch_truth_for_task("t1") is True


def test_has_fetch_truth_absent_or_empty_task_returns_false() -> None:
    store = StateStore(":memory:")
    store.append_fetch_truth(_ftr(task_id="t1"))
    assert store.has_fetch_truth_for_task("nope") is False
    # An empty task_id carries no key to match on.
    assert store.has_fetch_truth_for_task("") is False


def test_append_fetch_truth_appends_one_row_per_call() -> None:
    """Unlike the obligation-fork ledger there is no unique key: both rows land.

    Reproduces the JSONL writer's append-per-call semantics — a second marker
    for the same ``(task_id, pr_number)`` is a distinct row, not a no-op.
    """
    store = StateStore(":memory:")
    store.append_fetch_truth(_ftr(task_id="t1", pr_number=1, head_sha="a"))
    store.append_fetch_truth(_ftr(task_id="t1", pr_number=1, head_sha="b"))
    assert _fetch_truth_row_count(store) == 2
    assert store.has_fetch_truth_for_task("t1") is True


def test_fetch_truth_relap_exists_matches_unlanded_head() -> None:
    store = StateStore(":memory:")
    store.append_fetch_truth(
        _ftr(task_id="t1", pr_number=7, is_ancestor=False, head_sha="deadbeef"),
    )
    assert store.fetch_truth_relap_exists(7, "deadbeef") is True
    # A different head SHA does not match.
    assert store.fetch_truth_relap_exists(7, "other") is False


def test_fetch_truth_relap_exists_false_for_ancestor_true() -> None:
    """An ``is_ancestor``-true marker proves landed, so it never diverts."""
    store = StateStore(":memory:")
    store.append_fetch_truth(
        _ftr(task_id="t1", pr_number=7, is_ancestor=True, head_sha="deadbeef"),
    )
    assert store.fetch_truth_relap_exists(7, "deadbeef") is False


def test_fetch_truth_relap_exists_false_for_empty_head() -> None:
    store = StateStore(":memory:")
    store.append_fetch_truth(
        _ftr(task_id="t1", pr_number=7, is_ancestor=False, head_sha=""),
    )
    # An unresolvable head must never divert; the '' default head never matches.
    assert store.fetch_truth_relap_exists(7, "") is False
    assert store.fetch_truth_relap_exists(7, "anything") is False


def test_seed_fetch_truth_once_preserves_append_order() -> None:
    """Seeding oldest-first stamps the durable marker and lands every row."""
    store = StateStore(":memory:")
    store.seed_fetch_truth_once(
        [
            _ftr(task_id="t1", pr_number=1, head_sha="old"),
            _ftr(task_id="t1", pr_number=2, head_sha="new"),
        ],
    )
    assert store.has_fetch_truth_for_task("t1") is True
    assert _fetch_truth_row_count(store) == 2
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_fetch_truth_seeded() is True


def test_seed_fetch_truth_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_fetch_truth_once([])
    assert store.has_fetch_truth_for_task("task-1") is False
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_fetch_truth_seeded() is True


def test_seed_fetch_truth_once_is_idempotent_and_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops.

    Regression for the append-only doubling race (proposal 106): the guarded
    seed re-reads the durable marker inside its ``BEGIN IMMEDIATE`` critical
    section, so a repeat call inserts nothing.
    """
    store = StateStore(":memory:")
    legacy = [
        _ftr(task_id="t1", pr_number=1),
        _ftr(task_id="t2", pr_number=2),
    ]
    store.seed_fetch_truth_once(legacy)
    store.seed_fetch_truth_once(legacy)
    assert _fetch_truth_row_count(store) == 2


def test_seed_fetch_truth_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_fetch_truth_once([])  # empty seed stamps the marker
    store.seed_fetch_truth_once([_ftr(task_id="t1", pr_number=1)])
    assert store.has_fetch_truth_for_task("t1") is False


def test_seed_fetch_truth_once_does_not_double_populated_store() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted."""
    store = StateStore(":memory:")
    store.append_fetch_truth(_ftr(task_id="t9", pr_number=9))
    store.seed_fetch_truth_once([_ftr(task_id="t1", pr_number=1)])
    assert store.has_fetch_truth_for_task("t1") is False
    assert store.has_fetch_truth_for_task("t9") is True
    assert _fetch_truth_row_count(store) == 1
    assert store.is_fetch_truth_seeded() is True


def test_fetch_truth_seed_marker_defaults_false_then_stamps() -> None:
    """An empty ``seed_fetch_truth_once`` still stamps the durable marker."""
    store = StateStore(":memory:")
    assert store.is_fetch_truth_seeded() is False
    store.seed_fetch_truth_once([])
    assert store.is_fetch_truth_seeded() is True


# ---------------------------------------------------------------------------
# SqliteFetchTruthLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------

_LEGACY_FETCH_TRUTH_DIR = Path("/fake/fetchtruth")
_FETCH_TRUTH_FILENAME = "fetch_truth_markers.jsonl"


def _seed_legacy_fetch_truth(
    *rows: tuple[str, int, bool, str],
) -> None:
    """Append legacy fetch-truth rows to the legacy JSONL (oldest-first)."""
    for task_id, pr_number, is_ancestor, head_sha in rows:
        record_fetch_truth(
            task_id,
            pr_number,
            is_ancestor,
            _LEGACY_FETCH_TRUTH_DIR,
            head_sha=head_sha,
        )


def test_fetch_truth_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_fetch_truth(
        ("t1", 1, False, "h1"),
        ("t2", 2, True, "h2"),
    )
    ledger = SqliteFetchTruthLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    assert ledger.has("t1") is True
    assert ledger.has("t2") is True
    # The unlanded (is_ancestor False) marker diverts; the landed one does not.
    assert ledger.relap(1, "h1") is True
    assert ledger.relap(2, "h2") is False


def test_fetch_truth_adapter_seeds_legacy_row_with_empty_head(
    mem_fs: dict[str, str],
) -> None:
    """A legacy three-field row (no head) seeds with the '' default head."""
    _seed_legacy_fetch_truth(("t1", 1, False, ""))
    ledger = SqliteFetchTruthLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    assert ledger.has("t1") is True
    # An empty stored head never satisfies a relap probe.
    assert ledger.relap(1, "anything") is False


def test_fetch_truth_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_fetch_truth(("t1", 1, False, "h1"))
    store = StateStore(":memory:")
    first = SqliteFetchTruthLedger(
        store, legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    assert first.has("t1") is True
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteFetchTruthLedger(
        store, legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    assert second.has("t1") is True
    assert _fetch_truth_row_count(store) == 1


def test_fetch_truth_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_fetch_truth(("t1", 1, False, "h1"))
    ledger = SqliteFetchTruthLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    assert ledger.has("t1") is True  # seeds and flips the in-memory guard
    # A second access on the same adapter must not re-run the seed check.
    assert ledger.has("t1") is True


def test_fetch_truth_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty and legacy present."""
    _seed_legacy_fetch_truth(("t1", 1, False, "h1"))
    store = StateStore(":memory:")
    store.seed_fetch_truth_once([])  # empty seed stamps the marker
    ledger = SqliteFetchTruthLedger(
        store, legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    assert ledger.has("t1") is False  # marker blocks the re-seed
    assert store.is_fetch_truth_seeded() is True


def test_fetch_truth_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteFetchTruthLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.has("t1") is False


def test_fetch_truth_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteFetchTruthLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/fetchtruth/missing"),
    )
    assert ledger.has("t1") is False


def test_fetch_truth_adapter_no_seed_when_legacy_jsonl_empty(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_FETCH_TRUTH_DIR / _FETCH_TRUTH_FILENAME)] = ""
    ledger = SqliteFetchTruthLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    assert ledger.has("t1") is False


def test_fetch_truth_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_fetch_truth(("t1", 1, False, "h1"))
    store = StateStore(":memory:")
    store.append_fetch_truth(_ftr(task_id="t5", pr_number=5))
    ledger = SqliteFetchTruthLedger(
        store, legacy_jsonl_dir=_LEGACY_FETCH_TRUTH_DIR,
    )
    # Populated but unmarked → the seed no-ops; t1 never enters, t5 remains.
    assert ledger.has("t1") is False
    assert ledger.has("t5") is True


# ---------------------------------------------------------------------------
# SqliteFetchTruthLedger adapter — record / has / relap
# ---------------------------------------------------------------------------


def test_fetch_truth_adapter_record_persists() -> None:
    ledger = SqliteFetchTruthLedger(StateStore(":memory:"))
    ledger.record("t1", 1, False, head_sha="h1")
    assert ledger.has("t1") is True
    assert ledger.relap(1, "h1") is True


def test_fetch_truth_adapter_record_defaults_empty_head() -> None:
    ledger = SqliteFetchTruthLedger(StateStore(":memory:"))
    ledger.record("t1", 1, False)
    assert ledger.has("t1") is True
    # No head resolved → no relap divert.
    assert ledger.relap(1, "h1") is False


def test_fetch_truth_adapter_record_skips_empty_task_id() -> None:
    ledger = SqliteFetchTruthLedger(StateStore(":memory:"))
    ledger.record("", 1, False, head_sha="h1")
    # Mirrors the JSONL writer's guard: a keyless marker is never persisted.
    assert ledger.has("") is False
    assert _fetch_truth_row_count(ledger._store) == 0


def test_fetch_truth_adapter_record_appends_no_dedupe() -> None:
    ledger = SqliteFetchTruthLedger(StateStore(":memory:"))
    ledger.record("t1", 1, False, head_sha="a")
    ledger.record("t1", 1, False, head_sha="b")
    # No unique key — both rows persist, matching the JSONL append-per-call.
    assert _fetch_truth_row_count(ledger._store) == 2


# ---------------------------------------------------------------------------
# Preflight-pass ledger — keyed-replace store (proposal 106 Phase B Step 5)
# ---------------------------------------------------------------------------


def _preflight_row_count(store: StateStore) -> int:
    """Return the row count of the preflight-pass table (test-only probe)."""
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM preflight_passes",
        ).fetchone()[0],
    )


def test_state_store_creates_preflight_passes_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='preflight_passes'",
    )
    assert cur.fetchone() is not None


def test_record_and_should_skip_preflight_round_trip() -> None:
    store = StateStore(":memory:")
    store.record_preflight_pass(100, "head000")
    assert store.should_skip_preflight(100, "head000") is True
    # A different head for the same PR does not match.
    assert store.should_skip_preflight(100, "other") is False
    # An unrecorded PR never skips.
    assert store.should_skip_preflight(999, "head000") is False


def test_record_preflight_pass_replaces_by_key() -> None:
    """The ``pr_number`` primary key means the latest recorded head wins."""
    store = StateStore(":memory:")
    store.record_preflight_pass(100, "old")
    store.record_preflight_pass(100, "new")
    assert _preflight_row_count(store) == 1
    assert store.should_skip_preflight(100, "new") is True
    # The superseded head no longer skips.
    assert store.should_skip_preflight(100, "old") is False


def test_should_skip_preflight_empty_head_never_skips() -> None:
    store = StateStore(":memory:")
    store.record_preflight_pass(100, "head000")
    # An unresolvable head must never suppress remediation.
    assert store.should_skip_preflight(100, "") is False


def test_seed_preflight_passes_once_seeds_and_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_preflight_passes_once(
        [
            PreflightPassRecord(pr_number=1, head_sha="h1"),
            PreflightPassRecord(pr_number=2, head_sha="h2"),
        ],
    )
    assert store.should_skip_preflight(1, "h1") is True
    assert store.should_skip_preflight(2, "h2") is True
    assert _preflight_row_count(store) == 2
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_preflight_passes_seeded() is True


def test_seed_preflight_passes_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    assert store.is_preflight_passes_seeded() is False
    store.seed_preflight_passes_once([])
    # Still marks so a later start's fast-path skips (nothing to re-seed).
    assert store.is_preflight_passes_seeded() is True
    assert _preflight_row_count(store) == 0


def test_seed_preflight_passes_once_is_idempotent_and_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [
        PreflightPassRecord(pr_number=1, head_sha="h1"),
        PreflightPassRecord(pr_number=2, head_sha="h2"),
    ]
    store.seed_preflight_passes_once(legacy)
    store.seed_preflight_passes_once(legacy)
    assert _preflight_row_count(store) == 2


def test_seed_preflight_passes_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_preflight_passes_once([])  # empty seed stamps the marker
    store.seed_preflight_passes_once(
        [PreflightPassRecord(pr_number=1, head_sha="h1")],
    )
    assert store.should_skip_preflight(1, "h1") is False


def test_seed_preflight_passes_once_does_not_overwrite_populated_store() -> None:
    """A crash-populated-but-unmarked store keeps its live newer heads.

    The empty-table guard means a re-seed never clobbers a live passing head
    (here PR 1 at ``live``) with a stale legacy row (PR 1 at ``stale``).
    """
    store = StateStore(":memory:")
    store.record_preflight_pass(1, "live")
    store.seed_preflight_passes_once(
        [PreflightPassRecord(pr_number=1, head_sha="stale")],
    )
    assert store.should_skip_preflight(1, "live") is True
    assert store.should_skip_preflight(1, "stale") is False
    assert _preflight_row_count(store) == 1
    assert store.is_preflight_passes_seeded() is True


# ---------------------------------------------------------------------------
# SqlitePreflightPassStore adapter — one-time seed from legacy JSON
# ---------------------------------------------------------------------------

_LEGACY_PREFLIGHT_DIR = Path("/fake/preflight")


def test_preflight_adapter_seeds_from_legacy_json(mem_fs: dict[str, str]) -> None:
    record_preflight_pass(1, "h1", _LEGACY_PREFLIGHT_DIR)
    record_preflight_pass(2, "h2", _LEGACY_PREFLIGHT_DIR)
    ledger = SqlitePreflightPassStore(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_PREFLIGHT_DIR,
    )
    assert ledger.should_skip(1, "h1") is True
    assert ledger.should_skip(2, "h2") is True
    assert ledger.should_skip(1, "wrong") is False


def test_preflight_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    record_preflight_pass(1, "h1", _LEGACY_PREFLIGHT_DIR)
    store = StateStore(":memory:")
    first = SqlitePreflightPassStore(
        store, legacy_json_dir=_LEGACY_PREFLIGHT_DIR,
    )
    assert first.should_skip(1, "h1") is True
    # A second adapter over the already-seeded store must not re-seed.
    second = SqlitePreflightPassStore(
        store, legacy_json_dir=_LEGACY_PREFLIGHT_DIR,
    )
    assert second.should_skip(1, "h1") is True
    assert _preflight_row_count(store) == 1


def test_preflight_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    record_preflight_pass(1, "h1", _LEGACY_PREFLIGHT_DIR)
    ledger = SqlitePreflightPassStore(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_PREFLIGHT_DIR,
    )
    assert ledger.should_skip(1, "h1") is True  # seeds and flips the guard
    # A second access on the same adapter must not re-run the seed check.
    assert ledger.should_skip(1, "h1") is True


def test_preflight_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty with legacy present."""
    record_preflight_pass(1, "h1", _LEGACY_PREFLIGHT_DIR)
    store = StateStore(":memory:")
    store.seed_preflight_passes_once([])  # empty seed stamps the marker
    ledger = SqlitePreflightPassStore(
        store, legacy_json_dir=_LEGACY_PREFLIGHT_DIR,
    )
    assert ledger.should_skip(1, "h1") is False  # marker blocks the re-seed
    assert store.is_preflight_passes_seeded() is True


def test_preflight_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqlitePreflightPassStore(
        StateStore(":memory:"), legacy_json_dir=None,
    )
    assert ledger.should_skip(1, "h1") is False


def test_preflight_adapter_no_seed_when_legacy_json_absent() -> None:
    ledger = SqlitePreflightPassStore(
        StateStore(":memory:"),
        legacy_json_dir=Path("/fake/preflight/missing"),
    )
    assert ledger.should_skip(1, "h1") is False


def test_preflight_adapter_record_upserts_through_store() -> None:
    ledger = SqlitePreflightPassStore(StateStore(":memory:"))
    ledger.record(5, "h5")
    assert ledger.should_skip(5, "h5") is True
    # A later record for the same PR replaces the head (keyed replace).
    ledger.record(5, "h5b")
    assert ledger.should_skip(5, "h5") is False
    assert ledger.should_skip(5, "h5b") is True
    assert _preflight_row_count(ledger._store) == 1


# ---------------------------------------------------------------------------
# Dropped-directive replay ledger — bounded RMW store (Step 5)
# ---------------------------------------------------------------------------

_DROPPED_NOW = "2026-07-09T00:00:00+00:00"
_LEGACY_DROPPED_DIR = Path("/fake/dropped")


def _dropped_directive_row_count(store: StateStore) -> int:
    """Return the row count of the dropped-directives table (test-only probe)."""
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM dropped_directives",
        ).fetchone()[0],
    )


def _dd(
    text: str,
    *,
    prs: tuple[int, ...] = (),
    shas: tuple[str, ...] = (),
) -> DroppedDirective:
    """Build a :class:`DroppedDirective` with a fixed ``recorded_at``."""
    return DroppedDirective(
        text=text,
        source="s",
        pr_numbers=prs,
        shas=shas,
        recorded_at=_DROPPED_NOW,
    )


def _dead_never(_entry: DroppedDirective) -> bool:
    """An ``is_dead`` predicate that keeps every entry live."""
    return False


def test_state_store_creates_dropped_directives_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='dropped_directives'",
    )
    assert cur.fetchone() is not None


def test_offer_dropped_directive_appends_fifo() -> None:
    store = StateStore(":memory:")
    store.offer_dropped_directive(_dd("first", prs=(1,)))
    store.offer_dropped_directive(_dd("second", prs=(2,)))
    # Oldest-first replay (nothing dead) re-offers the earliest offered entry.
    assert store.replay_dropped_directive(_dead_never) == "first"
    assert _dropped_directive_row_count(store) == 1


def test_offer_dropped_directive_bounds_to_max_entries() -> None:
    store = StateStore(":memory:")
    for i in range(_MAX_ENTRIES + 5):
        store.offer_dropped_directive(_dd(f"d{i}", prs=(i,)))
    assert _dropped_directive_row_count(store) == _MAX_ENTRIES
    # The five oldest were dropped; the oldest surviving row is d5.
    assert store.replay_dropped_directive(_dead_never) == "d5"


def test_replay_dropped_directive_empty_returns_none() -> None:
    store = StateStore(":memory:")
    assert store.replay_dropped_directive(_dead_never) is None


def test_replay_dropped_directive_reads_inside_write_transaction() -> None:
    """The load of every banked row runs inside the same write transaction as
    the deletes, so a concurrent consumer cannot replay the same directive
    twice."""
    store = StateStore(":memory:")
    store.offer_dropped_directive(_dd("live-a", prs=(1,)))
    store.offer_dropped_directive(_dd("live-b", prs=(2,)))
    observed = _record_select_in_transaction(store)

    assert store.replay_dropped_directive(_dead_never) == "live-a"
    assert observed[0] is True
    assert store._conn.in_transaction is False


def test_replay_dropped_directive_empty_still_commits_its_transaction() -> None:
    """The no-rows ``return None`` returns from inside the open write
    transaction, which the context manager still commits."""
    store = StateStore(":memory:")
    observed = _record_select_in_transaction(store)

    assert store.replay_dropped_directive(_dead_never) is None
    assert observed[0] is True
    assert store._conn.in_transaction is False


def test_replay_dropped_directive_prunes_dead_and_offers_oldest_live() -> None:
    store = StateStore(":memory:")
    store.offer_dropped_directive(_dd("dead", prs=(100,)))
    store.offer_dropped_directive(_dd("live-a", prs=(200,)))
    store.offer_dropped_directive(_dd("live-b", prs=(300,)))

    def is_dead(entry: DroppedDirective) -> bool:
        return 100 in entry.pr_numbers

    assert store.replay_dropped_directive(is_dead) == "live-a"
    # The dead entry is pruned and the offered live entry consumed → one left.
    assert _dropped_directive_row_count(store) == 1
    assert store.replay_dropped_directive(_dead_never) == "live-b"


def test_replay_dropped_directive_all_dead_clears_and_returns_none() -> None:
    store = StateStore(":memory:")
    store.offer_dropped_directive(_dd("a", prs=(1,)))
    store.offer_dropped_directive(_dd("b", prs=(2,)))

    def always_dead(_entry: DroppedDirective) -> bool:
        return True

    assert store.replay_dropped_directive(always_dead) is None
    assert _dropped_directive_row_count(store) == 0


def test_seed_dropped_directives_once_seeds_and_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_dropped_directives_once(
        [_dd("a", prs=(1,)), _dd("b", shas=("abc",))],
    )
    assert _dropped_directive_row_count(store) == 2
    # The atomic seed stamps the durable marker in the same transaction.
    assert store.is_dropped_directives_seeded() is True
    # Oldest-first order is preserved by the AUTOINCREMENT id.
    assert store.replay_dropped_directive(_dead_never) == "a"


def test_seed_dropped_directives_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    assert store.is_dropped_directives_seeded() is False
    store.seed_dropped_directives_once([])
    assert store.is_dropped_directives_seeded() is True
    assert _dropped_directive_row_count(store) == 0


def test_seed_dropped_directives_once_is_idempotent_and_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = [_dd("a", prs=(1,)), _dd("b", prs=(2,))]
    store.seed_dropped_directives_once(legacy)
    store.seed_dropped_directives_once(legacy)
    assert _dropped_directive_row_count(store) == 2


def test_seed_dropped_directives_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_dropped_directives_once([])  # empty seed stamps the marker
    store.seed_dropped_directives_once([_dd("late", prs=(1,))])
    assert _dropped_directive_row_count(store) == 0


def test_seed_dropped_directives_once_does_not_double_populated_store() -> None:
    """A crash-populated-but-unmarked store is not re-seeded (empty-table guard)."""
    store = StateStore(":memory:")
    store.offer_dropped_directive(_dd("live", prs=(1,)))
    store.seed_dropped_directives_once([_dd("stale", prs=(2,))])
    assert _dropped_directive_row_count(store) == 1
    assert store.replay_dropped_directive(_dead_never) == "live"
    assert store.is_dropped_directives_seeded() is True


# ---------------------------------------------------------------------------
# SqliteDroppedDirectiveLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------


def _seed_legacy_dropped(*entries: DroppedDirective) -> None:
    """Write *entries* into the legacy JSONL via the default JSONL adapter."""
    ledger = JsonlDroppedDirectiveLedger(_LEGACY_DROPPED_DIR)
    for entry in entries:
        ledger.offer(entry)


def test_dropped_adapter_seeds_from_legacy_jsonl(mem_fs: dict[str, str]) -> None:
    _seed_legacy_dropped(_dd("a", prs=(1,)), _dd("b", prs=(2,)))
    ledger = SqliteDroppedDirectiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DROPPED_DIR,
    )
    # First replay triggers the one-time seed, then re-offers oldest-first.
    assert ledger.replay(_dead_never) == "a"
    assert ledger.replay(_dead_never) == "b"


def test_dropped_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_dropped(_dd("a", prs=(1,)))
    store = StateStore(":memory:")
    first = SqliteDroppedDirectiveLedger(
        store, legacy_jsonl_dir=_LEGACY_DROPPED_DIR,
    )
    first.offer(_dd("b", prs=(2,)))  # seeds from legacy, then appends
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteDroppedDirectiveLedger(
        store, legacy_jsonl_dir=_LEGACY_DROPPED_DIR,
    )
    second.offer(_dd("c", prs=(3,)))
    assert _dropped_directive_row_count(store) == 3


def test_dropped_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_dropped(_dd("a", prs=(1,)))
    ledger = SqliteDroppedDirectiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_DROPPED_DIR,
    )
    assert ledger.replay(_dead_never) == "a"  # seeds + flips the guard
    # A second access on the same adapter must not re-run the seed check.
    assert ledger.replay(_dead_never) is None


def test_dropped_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty with legacy present."""
    _seed_legacy_dropped(_dd("a", prs=(1,)))
    store = StateStore(":memory:")
    store.seed_dropped_directives_once([])  # empty seed stamps the marker
    ledger = SqliteDroppedDirectiveLedger(
        store, legacy_jsonl_dir=_LEGACY_DROPPED_DIR,
    )
    assert ledger.replay(_dead_never) is None  # marker blocks the re-seed
    assert store.is_dropped_directives_seeded() is True


def test_dropped_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteDroppedDirectiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.replay(_dead_never) is None


def test_dropped_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteDroppedDirectiveLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/dropped/missing"),
    )
    assert ledger.replay(_dead_never) is None


def test_dropped_adapter_offer_and_replay_through_store() -> None:
    ledger = SqliteDroppedDirectiveLedger(StateStore(":memory:"))
    ledger.offer(_dd("only", prs=(7,)))
    assert ledger.replay(_dead_never) == "only"
    assert _dropped_directive_row_count(ledger._store) == 0


# ---------------------------------------------------------------------------
# Settled-verdicts pre-filter — keyed-replace collapse store (Step 5)
# ---------------------------------------------------------------------------

_LEGACY_SETTLED_DIR = Path("/fake/settled")


def _settled_verdicts_row_count(store: StateStore) -> int:
    """Return the row count of the settled-verdicts table (test-only probe)."""
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM settled_verdicts",
        ).fetchone()[0],
    )


def _settle(
    pr: int, *, kind: str = "merged", iteration: int = 1,
    task_id: str | None = None,
) -> SettledVerdictRecord:
    """Build a *settle* record (``kind`` merged/closed)."""
    return SettledVerdictRecord(
        pr_number=pr, settled=True, kind=kind, iteration=iteration,
        verdict_task_id=task_id,
    )


def _retract(pr: int, *, iteration: int = 2) -> SettledVerdictRecord:
    """Build a *retraction* record (voids a prior settle)."""
    return SettledVerdictRecord(
        pr_number=pr, settled=False, kind="", iteration=iteration,
        verdict_task_id=None,
    )


def test_state_store_creates_settled_verdicts_table() -> None:
    store = StateStore(":memory:")
    row = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='settled_verdicts'",
    ).fetchone()
    assert row is not None


def test_append_and_effective_merged_settled_prs_round_trip() -> None:
    store = StateStore(":memory:")
    store.append_settled_verdicts([
        _settle(100, kind="merged"),
        _settle(200, kind="closed"),
        _settle(300, kind="merged"),
    ])
    assert store.effective_settled_prs() == frozenset({100, 200, 300})
    # merged excludes the closed settle.
    assert store.merged_settled_prs() == frozenset({100, 300})


def test_append_settled_verdicts_keyed_replace_collapses_last_row_wins() -> None:
    """Successive appends for one PR collapse to a single keyed row, last wins."""
    store = StateStore(":memory:")
    store.append_settled_verdicts([_settle(100, kind="merged", iteration=1)])
    store.append_settled_verdicts([_retract(100, iteration=2)])
    assert store.effective_settled_prs() == frozenset()
    assert store.merged_settled_prs() == frozenset()
    assert _settled_verdicts_row_count(store) == 1  # keyed replace, not append
    store.append_settled_verdicts([_settle(100, kind="merged", iteration=3)])
    assert store.effective_settled_prs() == frozenset({100})
    assert store.merged_settled_prs() == frozenset({100})
    assert _settled_verdicts_row_count(store) == 1


def test_append_settled_verdicts_empty_is_noop() -> None:
    store = StateStore(":memory:")
    store.append_settled_verdicts([])
    assert _settled_verdicts_row_count(store) == 0


def test_seed_settled_verdicts_once_seeds_and_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_settled_verdicts_once(
        [_settle(100, kind="merged"), _settle(200, kind="closed")],
    )
    assert _settled_verdicts_row_count(store) == 2
    assert store.is_settled_verdicts_seeded() is True
    assert store.effective_settled_prs() == frozenset({100, 200})
    assert store.merged_settled_prs() == frozenset({100})


def test_seed_settled_verdicts_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    assert store.is_settled_verdicts_seeded() is False
    store.seed_settled_verdicts_once([])
    assert store.is_settled_verdicts_seeded() is True
    assert _settled_verdicts_row_count(store) == 0


def test_seed_settled_verdicts_once_is_idempotent_and_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = [_settle(100, kind="merged"), _settle(200, kind="closed")]
    store.seed_settled_verdicts_once(legacy)
    store.seed_settled_verdicts_once(legacy)
    assert _settled_verdicts_row_count(store) == 2


def test_seed_settled_verdicts_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_settled_verdicts_once([])  # empty seed stamps the marker
    store.seed_settled_verdicts_once([_settle(999, kind="merged")])
    assert _settled_verdicts_row_count(store) == 0


def test_seed_settled_verdicts_once_does_not_double_populated_store() -> None:
    """A crash-populated-but-unmarked store is not re-seeded (empty-table guard)."""
    store = StateStore(":memory:")
    store.append_settled_verdicts([_settle(100, kind="merged")])
    store.seed_settled_verdicts_once([_settle(200, kind="closed")])
    assert _settled_verdicts_row_count(store) == 1
    assert store.effective_settled_prs() == frozenset({100})
    assert store.is_settled_verdicts_seeded() is True


# ---------------------------------------------------------------------------
# SqliteSettledVerdictsLedger adapter — one-time seed from legacy JSONL
# ---------------------------------------------------------------------------


def _seed_legacy_settled(*records: SettledVerdictRecord) -> None:
    """Write *records* into the legacy JSONL via the default JSONL adapter."""
    JsonlSettledVerdictsLedger(_LEGACY_SETTLED_DIR).append(records)


def test_settled_verdicts_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_settled(_settle(100, kind="merged"), _settle(200, kind="closed"))
    ledger = SqliteSettledVerdictsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_SETTLED_DIR,
    )
    # First read triggers the one-time seed.
    assert ledger.effective_settled_prs() == frozenset({100, 200})
    assert ledger.merged_settled_prs() == frozenset({100})


def test_settled_verdicts_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_settled(_settle(100, kind="merged"))
    store = StateStore(":memory:")
    first = SqliteSettledVerdictsLedger(
        store, legacy_jsonl_dir=_LEGACY_SETTLED_DIR,
    )
    first.append([_settle(200, kind="closed")])  # seeds from legacy, then appends
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteSettledVerdictsLedger(
        store, legacy_jsonl_dir=_LEGACY_SETTLED_DIR,
    )
    second.append([_settle(300, kind="merged")])
    assert _settled_verdicts_row_count(store) == 3


def test_settled_verdicts_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_settled(_settle(100, kind="merged"))
    ledger = SqliteSettledVerdictsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_SETTLED_DIR,
    )
    assert ledger.effective_settled_prs() == frozenset({100})  # seeds + flips guard
    # A second access on the same adapter must not re-run the seed check.
    assert ledger.effective_settled_prs() == frozenset({100})


def test_settled_verdicts_adapter_durable_marker_blocks_reseed(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty with legacy present."""
    _seed_legacy_settled(_settle(100, kind="merged"))
    store = StateStore(":memory:")
    store.seed_settled_verdicts_once([])  # empty seed stamps the marker
    ledger = SqliteSettledVerdictsLedger(
        store, legacy_jsonl_dir=_LEGACY_SETTLED_DIR,
    )
    assert ledger.effective_settled_prs() == frozenset()  # marker blocks the re-seed
    assert store.is_settled_verdicts_seeded() is True


def test_settled_verdicts_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqliteSettledVerdictsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert ledger.effective_settled_prs() == frozenset()


def test_settled_verdicts_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    ledger = SqliteSettledVerdictsLedger(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/settled/missing"),
    )
    assert ledger.effective_settled_prs() == frozenset()


def test_settled_verdicts_adapter_append_and_read_through_store(
    mem_fs: dict[str, str],
) -> None:
    ledger = SqliteSettledVerdictsLedger(StateStore(":memory:"))
    ledger.append([_settle(700, kind="merged"), _settle(800, kind="closed")])
    assert ledger.effective_settled_prs() == frozenset({700, 800})
    assert ledger.merged_settled_prs() == frozenset({700})


# ---------------------------------------------------------------------------
# Cross-substrate parity — JSONL and SQLite agree on both collapse reads
# ---------------------------------------------------------------------------


def test_settled_verdicts_adapters_agree_across_settle_retract_resettle(
    mem_fs: dict[str, str],
) -> None:
    """The JSONL and SQLite adapters agree on both the effective and merged sets
    across a settle → retract → re-settle collapse, seeded from one legacy ledger.

    PR 100 (merge → retract → merge) reads settled+merged; PR 200 (closed settle)
    reads settled-not-merged; PR 300 (merge → retract) reads absent. The Python
    last-row-wins walk and the SQLite keyed-replace batch must land the same sets.
    """
    jsonl = JsonlSettledVerdictsLedger(_LEGACY_SETTLED_DIR)
    jsonl.append([
        _settle(100, kind="merged", iteration=1),
        _retract(100, iteration=2),
        _settle(100, kind="merged", iteration=3),
        _settle(200, kind="closed", iteration=1),
        _settle(300, kind="merged", iteration=1),
        _retract(300, iteration=2),
    ])
    sqlite = SqliteSettledVerdictsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_SETTLED_DIR,
    )
    assert jsonl.effective_settled_prs() == frozenset({100, 200})
    assert sqlite.effective_settled_prs() == jsonl.effective_settled_prs()
    assert jsonl.merged_settled_prs() == frozenset({100})
    assert sqlite.merged_settled_prs() == jsonl.merged_settled_prs()


def test_load_all_settled_verdicts_parses_and_tolerates_corruption(
    mem_fs: dict[str, str],
) -> None:
    """The seed source maps settle and retraction rows to typed records in file
    order, tolerating corruption exactly like the collapse walk: blank / malformed
    / non-dict / non-int-pr rows and damaged retractions are skipped; a settle row
    missing ``kind`` / ``settled_at_iteration`` / ``verdict_task_id`` degrades to
    ``kind=""`` / ``iteration=0`` / ``None``."""
    mem_fs[str(_LEGACY_SETTLED_DIR / "settled_verdicts.jsonl")] = (
        "\n"                                       # blank line
        "{not valid json}\n"                       # malformed
        "[1, 2, 3]\n"                              # non-dict row
        '{"pr_number": true}\n'                    # bool pr rejected
        '{"pr_number": "5"}\n'                     # string pr rejected
        '{"pr_number": 100, "settled_at_iteration": 3, "kind": "merged",'
        ' "verdict_task_id": "t1"}\n'
        '{"pr_number": 200, "settled_at_iteration": 4, "kind": "closed"}\n'
        '{"pr_number": 300}\n'                      # bare settle: kind "" / iter 0
        '{"pr_number": 400, "retracted_at_iteration": 9}\n'   # retraction
        '{"pr_number": 500, "retracted_at_iteration": "x"}\n'  # damaged, skipped
    )
    records = load_all_settled_verdicts(_LEGACY_SETTLED_DIR)
    assert records == [
        SettledVerdictRecord(100, True, "merged", 3, "t1"),
        SettledVerdictRecord(200, True, "closed", 4, None),
        SettledVerdictRecord(300, True, "", 0, None),
        SettledVerdictRecord(400, False, "", 9, None),
    ]


def test_load_all_settled_verdicts_absent_file_is_empty() -> None:
    assert load_all_settled_verdicts(Path("/fake/settled/none")) == []


def test_jsonl_settled_verdicts_append_empty_is_noop(
    mem_fs: dict[str, str],
) -> None:
    """The JSONL adapter's empty-batch append opens no file — the twin of the
    SQLite store's ``test_append_settled_verdicts_empty_is_noop``.

    ``append([])`` returns before touching the ledger, so no
    ``settled_verdicts.jsonl`` is created, and the read side still reads the
    absent ledger as the empty set.
    """
    ledger = JsonlSettledVerdictsLedger(_LEGACY_SETTLED_DIR)
    ledger.append([])
    assert str(_LEGACY_SETTLED_DIR / "settled_verdicts.jsonl") not in mem_fs
    assert ledger.effective_settled_prs() == frozenset()


def test_settled_verdicts_adapters_agree_on_kindless_settle_after_merged(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt ``[merged settle, kind-less settle]`` sequence for one PR reads
    not-merged under **both** adapters (proposal 106 parity fix).

    A kind-less settle row can only arise from external file corruption
    (production writers always set ``kind``). ``load_all_settled_verdicts`` and
    the SQLite keyed-replace seed normalize it to ``kind=""`` (not merged); the
    JSONL ``merged_settled_prs`` walk now degrades it the same way rather than
    treating it as a no-op that leaves the prior merged state standing — so the
    two collapse reads no longer diverge on this PR. Both still read the PR as
    *effectively settled* (the settle is not voided), only not *merged*.
    """
    mem_fs[str(_LEGACY_SETTLED_DIR / "settled_verdicts.jsonl")] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "merged"}\n'
        '{"pr_number": 100, "settled_at_iteration": 2}\n'  # kind-less settle
    )
    jsonl = JsonlSettledVerdictsLedger(_LEGACY_SETTLED_DIR)
    sqlite = SqliteSettledVerdictsLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_SETTLED_DIR,
    )
    assert jsonl.merged_settled_prs() == frozenset()
    assert sqlite.merged_settled_prs() == jsonl.merged_settled_prs()
    assert jsonl.effective_settled_prs() == frozenset({100})
    assert sqlite.effective_settled_prs() == jsonl.effective_settled_prs()


# ---------------------------------------------------------------------------
# Stand-down verdict ledger (proposal 106 Phase B Step 5) — append-only, no
# unique key, two on-disk row shapes read with a release-aware last-write-wins
# scan. Mirrors the fetch-truth append-only ledger's test coverage.
# ---------------------------------------------------------------------------

_LEGACY_STAND_DOWN_DIR = Path("/fake/stand_down")


def _sd_row_count(store: StateStore) -> int:
    """Return the row count of the stand-down table (test-only probe)."""
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM stand_down_verdicts",
        ).fetchone()[0],
    )


def _verdict(pr: int, head_sha: str, at: str, ahead: bool | None = None) -> StandDownRow:
    """Build a verdict :class:`StandDownRow` for the store-level tests."""
    return StandDownRow(
        pr=pr, head_sha=head_sha, recorded_at=at, branch_ahead_of_origin=ahead,
    )


def test_state_store_creates_stand_down_verdicts_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='stand_down_verdicts'",
    )
    assert cur.fetchone() is not None


def test_stand_down_append_verdict_and_find_round_trip() -> None:
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "sha1", "2026-01-01T00:00:00+00:00"))
    found = store.stand_down_find(1, "sha1")
    assert found == StandDownVerdict(
        pr=1, head_sha="sha1", recorded_at="2026-01-01T00:00:00+00:00",
    )
    # A different head SHA is the SHA-keyed auto-release: no match.
    assert store.stand_down_find(1, "other") is None
    assert store.stand_down_find(99, "sha1") is None


def test_stand_down_find_reads_branch_ahead_flag() -> None:
    """A stored ``1``/``0`` round-trips to ``True``/``False``; NULL to ``None``."""
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "a", "t", ahead=True))
    store.append_stand_down_verdict(_verdict(2, "b", "t", ahead=False))
    store.append_stand_down_verdict(_verdict(3, "c", "t", ahead=None))
    assert store.stand_down_find(1, "a").branch_ahead_of_origin is True
    assert store.stand_down_find(2, "b").branch_ahead_of_origin is False
    assert store.stand_down_find(3, "c").branch_ahead_of_origin is None


def test_stand_down_release_voids_earlier_verdict_last_write_wins() -> None:
    """A release row voids an earlier verdict; a later verdict re-matches."""
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "a", "t1"))
    store.append_stand_down_release(1, "t2")
    assert store.stand_down_find(1, "a") is None
    assert store.stand_down_has_live(1) is False
    # A verdict recorded on a later row re-arms the brake.
    store.append_stand_down_verdict(_verdict(1, "b", "t3"))
    assert store.stand_down_find(1, "b") is not None
    assert store.stand_down_has_live(1) is True


def test_stand_down_has_live_absent_pr_is_false() -> None:
    store = StateStore(":memory:")
    assert store.stand_down_has_live(1) is False


def test_stand_down_verdict_rows_excludes_release_rows() -> None:
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "a", "t1", ahead=True))
    store.append_stand_down_release(1, "t2")
    store.append_stand_down_verdict(_verdict(2, "b", "t3"))
    rows = store.stand_down_verdict_rows()
    assert rows == [
        StandDownRow(pr=1, head_sha="a", recorded_at="t1", branch_ahead_of_origin=True),
        StandDownRow(pr=2, head_sha="b", recorded_at="t3"),
    ]


def test_stand_down_written_count_counts_only_verdict_rows() -> None:
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "a", "t1"))
    store.append_stand_down_verdict(_verdict(1, "b", "t2"))
    store.append_stand_down_release(1, "t3")
    assert store.stand_down_written_count() == 2


def test_stand_down_recorded_timestamps_skips_empty_and_release() -> None:
    """Only rows carrying a non-empty ``recorded_at`` contribute a timestamp."""
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "a", "t1"))
    store.append_stand_down_release(1, "t2")  # release: no recorded_at
    store.append_stand_down_verdict(StandDownRow(pr=2, head_sha="b", recorded_at=""))
    assert store.stand_down_recorded_timestamps() == ["t1"]


def test_stand_down_banked_pr_ids_includes_release_row_prs() -> None:
    """A PR I ever stood down (verdict or release) is banked; NULL pr excluded."""
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(5, "a", "t1"))
    store.append_stand_down_release(6, "t2")  # release-only PR still banked
    assert store.stand_down_banked_pr_ids() == {5, 6}


def test_stand_down_verdict_count_for_pr_ignores_release_only() -> None:
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "a", "t1"))
    store.append_stand_down_verdict(_verdict(1, "b", "t3"))
    store.append_stand_down_release(1, "t4")
    store.append_stand_down_release(2, "t2")
    # Every verdict row for the PR counts; its release row does not.
    assert store.stand_down_verdict_count_for_pr(1) == 2
    # A PR with only a release row has no verdict row.
    assert store.stand_down_verdict_count_for_pr(2) == 0
    assert store.stand_down_verdict_count_for_pr(99) == 0


def test_seed_stand_down_verdicts_once_preserves_append_order() -> None:
    store = StateStore(":memory:")
    store.seed_stand_down_verdicts_once(
        [_verdict(1, "old", "t1"), _verdict(1, "new", "t2")],
    )
    assert _sd_row_count(store) == 2
    # AUTOINCREMENT id preserves append order for the release-aware scan.
    assert store.stand_down_find(1, "new") is not None
    assert store.is_stand_down_verdicts_seeded() is True


def test_seed_stand_down_verdicts_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    store.seed_stand_down_verdicts_once([])
    assert _sd_row_count(store) == 0
    assert store.is_stand_down_verdicts_seeded() is True


def test_seed_stand_down_verdicts_once_is_idempotent_and_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = [_verdict(1, "a", "t1"), _verdict(2, "b", "t2")]
    store.seed_stand_down_verdicts_once(legacy)
    store.seed_stand_down_verdicts_once(legacy)
    assert _sd_row_count(store) == 2


def test_seed_stand_down_verdicts_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_stand_down_verdicts_once([])  # stamps the marker over an empty table
    store.seed_stand_down_verdicts_once([_verdict(1, "a", "t1")])
    assert _sd_row_count(store) == 0


def test_seed_stand_down_verdicts_once_does_not_double_populated_store() -> None:
    """A store populated by a prior crashed run (unmarked) is not re-seeded."""
    store = StateStore(":memory:")
    store.append_stand_down_verdict(_verdict(1, "a", "t1"))
    store.seed_stand_down_verdicts_once([_verdict(2, "b", "t2")])
    # The empty-table guard blocks the insert, but the marker is still stamped.
    assert _sd_row_count(store) == 1
    assert store.is_stand_down_verdicts_seeded() is True


def test_stand_down_seed_marker_defaults_false_then_stamps() -> None:
    store = StateStore(":memory:")
    assert store.is_stand_down_verdicts_seeded() is False
    store.seed_stand_down_verdicts_once([])
    assert store.is_stand_down_verdicts_seeded() is True


def test_stand_down_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_STAND_DOWN_DIR / "stand_down_verdicts.jsonl")] = (
        '{"pr": 1, "head_sha": "a", "recorded_at": "t1", '
        '"branch_ahead_of_origin": true}\n'
        '{"pr": 1, "released_at": "t2"}\n'
    )
    adapter = SqliteStandDownVerdictLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_STAND_DOWN_DIR,
    )
    # First access seeds; the release voids the verdict head-agnostically.
    assert adapter.has_live(1) is False
    assert adapter.banked_pr_ids() == {1}
    assert adapter._store.is_stand_down_verdicts_seeded() is True


def test_stand_down_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(_LEGACY_STAND_DOWN_DIR / "stand_down_verdicts.jsonl")] = (
        '{"pr": 1, "head_sha": "a", "recorded_at": "t1"}\n'
    )
    adapter = SqliteStandDownVerdictLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_STAND_DOWN_DIR,
    )
    adapter.has_live(1)
    adapter.written_count()
    assert _sd_row_count(adapter._store) == 1


def test_stand_down_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    """Once ``_seed_checked`` is set, later calls skip the marker read entirely."""
    adapter = SqliteStandDownVerdictLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_STAND_DOWN_DIR,
    )
    adapter._seed_checked = True
    # No legacy file exists, but the short-circuit means no seed is attempted.
    assert adapter.verdict_count_for_pr(1) == 0


def test_stand_down_adapter_durable_marker_blocks_reseed_over_empty_store() -> None:
    """A pre-marked store is never seeded even with a legacy dir set."""
    store = StateStore(":memory:")
    store.seed_stand_down_verdicts_once([])  # stamp the marker
    adapter = SqliteStandDownVerdictLedger(
        store, legacy_jsonl_dir=_LEGACY_STAND_DOWN_DIR,
    )
    adapter.append_verdict(_verdict(1, "a", "t1"))
    assert adapter.find(1, "a") is not None


def test_stand_down_adapter_no_seed_without_legacy_dir() -> None:
    adapter = SqliteStandDownVerdictLedger(StateStore(":memory:"))
    adapter.append_release(3, "t1")
    assert adapter.has_live(3) is False
    assert adapter.banked_pr_ids() == {3}


def test_stand_down_adapter_no_seed_when_legacy_jsonl_absent(
    mem_fs: dict[str, str],
) -> None:
    adapter = SqliteStandDownVerdictLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_LEGACY_STAND_DOWN_DIR,
    )
    # The legacy file does not exist, so the seed lands zero rows but marks.
    assert adapter.verdict_rows() == []
    assert adapter._store.is_stand_down_verdicts_seeded() is True


def test_stand_down_adapter_recorded_timestamps_and_verdict_rows(
    mem_fs: dict[str, str],
) -> None:
    adapter = SqliteStandDownVerdictLedger(StateStore(":memory:"))
    adapter.append_verdict(_verdict(1, "a", "t1", ahead=True))
    assert adapter.recorded_timestamps() == ["t1"]
    assert adapter.verdict_rows() == [
        StandDownRow(pr=1, head_sha="a", recorded_at="t1", branch_ahead_of_origin=True),
    ]
    assert adapter.written_count() == 1


def test_load_all_stand_down_records_round_trips_both_shapes(
    mem_fs: dict[str, str],
) -> None:
    """The seed source keeps verdict rows, release rows, and skips corruption."""
    mem_fs[str(_LEGACY_STAND_DOWN_DIR / "stand_down_verdicts.jsonl")] = (
        '{"pr": 1, "head_sha": "a", "recorded_at": "t1", '
        '"branch_ahead_of_origin": false}\n'
        "\n"  # blank line, skipped
        '{"pr": 1, "released_at": "t2"}\n'
        "not json\n"
        "[1, 2, 3]\n"  # non-dict row, skipped
        '{"released_at": "t3"}\n'  # no pr, skipped
    )
    records = load_all_stand_down_records(_LEGACY_STAND_DOWN_DIR)
    assert records == [
        StandDownRow(
            pr=1, head_sha="a", recorded_at="t1", branch_ahead_of_origin=False,
        ),
        StandDownRow(pr=1, released_at="t2"),
    ]


def test_load_all_stand_down_records_absent_file_is_empty() -> None:
    assert load_all_stand_down_records(_LEGACY_STAND_DOWN_DIR) == []


def test_stand_down_jsonl_banked_pr_ids_tolerates_corruption(
    mem_fs: dict[str, str],
) -> None:
    """The JSONL ``banked_pr_ids`` scan skips blank / malformed / non-dict rows.

    A release row's PR is still banked (a PR I ever stood down), and a ``bool``
    ``pr`` is rejected because it subclasses ``int``.
    """
    mem_fs[str(_LEGACY_STAND_DOWN_DIR / "stand_down_verdicts.jsonl")] = (
        "\n"  # blank line, skipped
        "not json\n"  # JSONDecodeError, skipped
        "[1, 2]\n"  # non-dict row, skipped
        '{"pr": true}\n'  # bool pr rejected
        '{"pr": 5, "head_sha": "a", "recorded_at": "t1"}\n'
        '{"pr": 6, "released_at": "t2"}\n'  # release row still banks its PR
    )
    jsonl = JsonlStandDownVerdictLedger(_LEGACY_STAND_DOWN_DIR)
    assert jsonl.banked_pr_ids() == {5, 6}


def test_stand_down_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same verdict → release → re-verdict sequence yields identical reads.

    Runs the free functions over both a JSONL adapter and a SQLite adapter and
    asserts every read shape agrees — the core proposal-106 parity guarantee.
    """
    jsonl = JsonlStandDownVerdictLedger(Path("/fake/parity"))
    sqlite = SqliteStandDownVerdictLedger(StateStore(":memory:"))

    for backend in (jsonl, sqlite):
        record_stand_down(1, "a", backend, now="t1", branch_ahead_of_origin=True)
        record_stand_down(1, "b", backend, now="t2", branch_ahead_of_origin=False)
        release_stand_downs(1, backend, now="t3")
        record_stand_down(1, "c", backend, now="t4")
        record_stand_down(2, "x", backend, now="t5")

    # find: the release voided the a/b verdicts; only the later c verdict matches.
    assert jsonl.find(1, "a") == sqlite.find(1, "a") is None
    assert jsonl.find(1, "b") == sqlite.find(1, "b") is None
    assert jsonl.find(1, "c") == sqlite.find(1, "c")
    assert jsonl.find(1, "c") is not None
    assert jsonl.find(2, "x") == sqlite.find(2, "x")
    assert jsonl.find(1, "nope") == sqlite.find(1, "nope") is None

    # find respects the caller-side guards identically.
    assert find_stand_down(1, "c", jsonl) == find_stand_down(1, "c", sqlite)
    assert find_stand_down(0, "c", jsonl) == find_stand_down(0, "c", sqlite) is None

    assert jsonl.has_live(1) == sqlite.has_live(1) is True
    assert jsonl.has_live(2) == sqlite.has_live(2) is True
    assert jsonl.has_live(99) == sqlite.has_live(99) is False
    assert has_live_stand_down(1, jsonl) == has_live_stand_down(1, sqlite)

    assert jsonl.verdict_rows() == sqlite.verdict_rows()
    assert jsonl.written_count() == sqlite.written_count() == 4
    assert jsonl.recorded_timestamps() == sqlite.recorded_timestamps()
    assert jsonl.banked_pr_ids() == sqlite.banked_pr_ids() == {1, 2}
    # PR 1 carries three verdict rows (a, b, c); the release row is not counted.
    assert jsonl.verdict_count_for_pr(1) == sqlite.verdict_count_for_pr(1) == 3
    assert jsonl.verdict_count_for_pr(2) == sqlite.verdict_count_for_pr(2) == 1
    assert jsonl.verdict_count_for_pr(99) == sqlite.verdict_count_for_pr(99) == 0

    # The OwnedAct projection agrees too (chosen iff branch was ahead of origin).
    assert read_chosen_stand_down_acts(jsonl) == read_chosen_stand_down_acts(sqlite)


# ---------------------------------------------------------------------------
# Unconfirmed-own-landing first-seen sidecar (proposal 106 Phase B Step 5)
# ---------------------------------------------------------------------------

_LEGACY_LANDING_DIR = Path("/fake/landing-store")
_LANDING_SIDECAR_FILE = str(
    _LEGACY_LANDING_DIR / "unconfirmed_landing_first_seen.json",
)


def test_unconfirmed_landing_store_prune_and_touch_round_trip() -> None:
    """The store's one-transaction prune-and-touch round-trips; empty clears it."""
    store = StateStore(":memory:")
    # Fresh touch stamps both SHAs at the current iteration and persists them.
    assert store.prune_and_touch_unconfirmed_landing(["aaa", "bbb"], 5) == {
        "aaa": 5, "bbb": 5,
    }
    assert store.load_unconfirmed_landing_seen() == {"aaa": 5, "bbb": 5}
    # A re-touch preserves the earlier first-seen for a still-observed SHA,
    # stamps the current iteration on a newly-seen one, and prunes the SHA no
    # longer observed ("bbb") — the load-prune-write is one atomic store method.
    assert store.prune_and_touch_unconfirmed_landing(["aaa", "ccc"], 9) == {
        "aaa": 5, "ccc": 9,
    }
    assert store.load_unconfirmed_landing_seen() == {"aaa": 5, "ccc": 9}
    # An empty observed set clears the table — the SQLite analogue of the JSON
    # adapter's ``{}`` write on a full prune.
    assert store.prune_and_touch_unconfirmed_landing([], 12) == {}
    assert store.load_unconfirmed_landing_seen() == {}


def test_seed_unconfirmed_landing_once_is_idempotent() -> None:
    """The one-time seed stamps a durable marker; a second seed is a no-op."""
    store = StateStore(":memory:")
    store.seed_unconfirmed_landing_once({"aaa": 1})
    assert store.load_unconfirmed_landing_seen() == {"aaa": 1}
    assert store.is_unconfirmed_landing_seeded() is True
    # A second seed (even with different data) is blocked by the marker.
    store.seed_unconfirmed_landing_once({"bbb": 2})
    assert store.load_unconfirmed_landing_seen() == {"aaa": 1}


def test_unconfirmed_landing_jsonl_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same ``prune_and_touch`` sequence yields identical maps on both backends.

    Fresh touch (first-seen stamped this cycle), prune-away of a now-landed SHA
    while preserving an earlier first-seen on the survivors, and a full prune to
    the empty map must all agree between the JSON sidecar and the SQLite
    keyed-replace table — the core proposal-106 parity guarantee.
    """
    json_store = JsonUnconfirmedOwnLandingStore(_LEGACY_LANDING_DIR)
    sqlite_store = SqliteUnconfirmedOwnLandingStore(StateStore(":memory:"))

    # Fresh touch: both SHAs first seen at iteration 5.
    assert json_store.prune_and_touch(["aaa", "bbb"], 5) == {"aaa": 5, "bbb": 5}
    assert sqlite_store.prune_and_touch(["aaa", "bbb"], 5) == {"aaa": 5, "bbb": 5}

    # Re-touch at a later iteration preserves the earlier first-seen for the SHAs
    # still observed and stamps the new SHA at the current iteration; the SHA no
    # longer observed ("bbb", now landed) is pruned out.
    assert json_store.prune_and_touch(["aaa", "ccc"], 9) == {"aaa": 5, "ccc": 9}
    assert sqlite_store.prune_and_touch(["aaa", "ccc"], 9) == {"aaa": 5, "ccc": 9}

    # A full prune (every SHA landed) empties the map on both substrates.
    assert json_store.prune_and_touch([], 12) == {}
    assert sqlite_store.prune_and_touch([], 12) == {}


def test_load_all_unconfirmed_landing_seen_parses_and_filters_junk(
    mem_fs: dict[str, str],
) -> None:
    """The seed source returns the ``{sha: first_seen}`` map, dropping junk entries.

    Reuses the corrupt-tolerant loader: non-string keys are impossible in JSON
    object form, but non-int and boolean values (``bool`` subclasses ``int``) are
    filtered so a corrupted ``true`` is never read as iteration 1/0.
    """
    mem_fs[_LANDING_SIDECAR_FILE] = json.dumps({
        "aaa": 3,
        "bbb": 7,
        "ccc": True,   # bool rejected
        "ddd": "x",    # non-int rejected
    })
    assert load_all_unconfirmed_landing_seen(_LEGACY_LANDING_DIR) == {
        "aaa": 3, "bbb": 7,
    }


def test_load_all_unconfirmed_landing_seen_corrupt_file_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """A malformed JSON sidecar reads as the empty map, not a crash."""
    mem_fs[_LANDING_SIDECAR_FILE] = "{not valid json"
    assert load_all_unconfirmed_landing_seen(_LEGACY_LANDING_DIR) == {}


def test_load_all_unconfirmed_landing_seen_absent_file_is_empty() -> None:
    assert load_all_unconfirmed_landing_seen(Path("/fake/landing/none")) == {}


def test_unconfirmed_landing_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    """Enabling the flag seeds the store once from the legacy JSON sidecar.

    The first ``prune_and_touch`` triggers the one-time seed; observing both
    seeded SHAs preserves their legacy first-seen iterations rather than restamping
    them at the current iteration.
    """
    mem_fs[_LANDING_SIDECAR_FILE] = json.dumps({"aaa": 2, "bbb": 4})
    store = StateStore(":memory:")
    adapter = SqliteUnconfirmedOwnLandingStore(
        store, legacy_jsonl_dir=_LEGACY_LANDING_DIR,
    )
    assert adapter.prune_and_touch(["aaa", "bbb"], 10) == {"aaa": 2, "bbb": 4}
    assert store.is_unconfirmed_landing_seeded() is True


def test_unconfirmed_landing_adapter_durable_marker_blocks_reseed(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty with legacy present."""
    mem_fs[_LANDING_SIDECAR_FILE] = json.dumps({"aaa": 2})
    store = StateStore(":memory:")
    store.seed_unconfirmed_landing_once({})  # empty seed stamps the marker
    adapter = SqliteUnconfirmedOwnLandingStore(
        store, legacy_jsonl_dir=_LEGACY_LANDING_DIR,
    )
    # The marker blocks the re-seed, so legacy {"aaa": 2} is not imported; a fresh
    # touch stamps "aaa" at the current iteration instead of the legacy 2.
    assert adapter.prune_and_touch(["aaa"], 8) == {"aaa": 8}
    assert store.is_unconfirmed_landing_seeded() is True


def test_unconfirmed_landing_adapter_no_seed_without_legacy_dir() -> None:
    adapter = SqliteUnconfirmedOwnLandingStore(
        StateStore(":memory:"), legacy_jsonl_dir=None,
    )
    assert adapter.prune_and_touch(["zzz"], 5) == {"zzz": 5}


def test_unconfirmed_landing_adapter_no_seed_when_legacy_jsonl_absent() -> None:
    adapter = SqliteUnconfirmedOwnLandingStore(
        StateStore(":memory:"),
        legacy_jsonl_dir=Path("/fake/landing/missing"),
    )
    assert adapter.prune_and_touch(["zzz"], 5) == {"zzz": 5}


# ---------------------------------------------------------------------------
# Orphaned-cure first-seen sidecar (proposal 106 Phase B Step 5)
# ---------------------------------------------------------------------------

_LEGACY_CURE_DIR = Path("/fake/cure-store")
_CURE_SIDECAR_FILE = str(
    _LEGACY_CURE_DIR / "orphaned_cure_first_seen.json",
)


def test_prune_and_touch_unconfirmed_landing_reads_inside_write_transaction() -> None:
    """The load of the first-seen map runs inside the same write transaction as
    the whole-table replace, so a row a concurrent writer inserts between them
    cannot be silently erased."""
    store = StateStore(":memory:")
    store.prune_and_touch_unconfirmed_landing(["aaa"], 5)
    observed = _record_select_in_transaction(store)

    assert store.prune_and_touch_unconfirmed_landing(["aaa", "bbb"], 9) == {
        "aaa": 5, "bbb": 9,
    }
    assert observed[0] is True
    assert store._conn.in_transaction is False


def test_orphaned_cure_store_prune_and_touch_round_trip() -> None:
    """The store's one-transaction prune-and-touch round-trips; empty clears it."""
    store = StateStore(":memory:")
    # Fresh touch stamps both plan ids at the current iteration and persists them.
    assert store.prune_and_touch_orphaned_cure(["ep1", "ep2"], 5) == {
        "ep1": 5, "ep2": 5,
    }
    assert store.load_orphaned_cure_seen() == {"ep1": 5, "ep2": 5}
    # A re-touch preserves the earlier first-seen for a still-observed plan id,
    # stamps the current iteration on a newly-seen one, and prunes the plan id no
    # longer observed ("ep2") — the load-prune-write is one atomic store method.
    assert store.prune_and_touch_orphaned_cure(["ep1", "ep3"], 9) == {
        "ep1": 5, "ep3": 9,
    }
    assert store.load_orphaned_cure_seen() == {"ep1": 5, "ep3": 9}
    # An empty observed set clears the table — the SQLite analogue of the JSON
    # adapter's ``{}`` write on a full prune.
    assert store.prune_and_touch_orphaned_cure([], 12) == {}
    assert store.load_orphaned_cure_seen() == {}


def test_prune_and_touch_orphaned_cure_reads_inside_write_transaction() -> None:
    """The load of the first-seen map runs inside the same write transaction as
    the whole-table replace, so a plan id a concurrent writer inserts between
    them cannot be silently erased."""
    store = StateStore(":memory:")
    store.prune_and_touch_orphaned_cure(["ep1"], 5)
    observed = _record_select_in_transaction(store)

    assert store.prune_and_touch_orphaned_cure(["ep1", "ep2"], 9) == {
        "ep1": 5, "ep2": 9,
    }
    assert observed[0] is True
    assert store._conn.in_transaction is False


def test_seed_orphaned_cure_once_is_idempotent() -> None:
    """The one-time seed stamps a durable marker; a second seed is a no-op."""
    store = StateStore(":memory:")
    store.seed_orphaned_cure_once({"ep1": 1})
    assert store.load_orphaned_cure_seen() == {"ep1": 1}
    assert store.is_orphaned_cure_seeded() is True
    # A second seed (even with different data) is blocked by the marker.
    store.seed_orphaned_cure_once({"ep2": 2})
    assert store.load_orphaned_cure_seen() == {"ep1": 1}


def test_orphaned_cure_json_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same ``prune_and_touch`` sequence yields identical maps on both backends.

    Fresh touch, prune-away of a now-departed plan id while preserving an earlier
    first-seen on the survivors, and a full prune to the empty map must all agree
    between the JSON sidecar and the SQLite keyed-replace table — the core
    proposal-106 parity guarantee. The read-only ``load`` accessor (the second
    consumer this map has, unlike its single-consumer sibling) must agree too.
    """
    json_store = JsonOrphanedCureStore(_LEGACY_CURE_DIR)
    sqlite_store = SqliteOrphanedCureStore(StateStore(":memory:"))

    # Fresh touch: both plan ids first seen at iteration 5.
    assert json_store.prune_and_touch(["ep1", "ep2"], 5) == {"ep1": 5, "ep2": 5}
    assert sqlite_store.prune_and_touch(["ep1", "ep2"], 5) == {"ep1": 5, "ep2": 5}
    # The read-only ``load`` returns the same map on both substrates.
    assert json_store.load() == {"ep1": 5, "ep2": 5}
    assert sqlite_store.load() == {"ep1": 5, "ep2": 5}

    # Re-touch at a later iteration preserves the earlier first-seen for the plan
    # ids still observed and stamps the new one at the current iteration; the plan
    # id no longer observed ("ep2", now merged) is pruned out.
    assert json_store.prune_and_touch(["ep1", "ep3"], 9) == {"ep1": 5, "ep3": 9}
    assert sqlite_store.prune_and_touch(["ep1", "ep3"], 9) == {"ep1": 5, "ep3": 9}

    # A full prune (every cure departed) empties the map on both substrates.
    assert json_store.prune_and_touch([], 12) == {}
    assert sqlite_store.prune_and_touch([], 12) == {}
    assert json_store.load() == {}
    assert sqlite_store.load() == {}


def test_load_all_orphaned_cure_seen_parses_and_filters_junk(
    mem_fs: dict[str, str],
) -> None:
    """The seed source returns the ``{plan_id: first_seen}`` map, dropping junk.

    Reuses the corrupt-tolerant loader: non-int and boolean values (``bool``
    subclasses ``int``) are filtered so a corrupted ``true`` is never read as
    iteration 1/0.
    """
    mem_fs[_CURE_SIDECAR_FILE] = json.dumps({
        "ep1": 3,
        "ep2": 7,
        "ep3": True,   # bool rejected
        "ep4": "x",    # non-int rejected
    })
    assert load_all_orphaned_cure_seen(_LEGACY_CURE_DIR) == {
        "ep1": 3, "ep2": 7,
    }


def test_load_all_orphaned_cure_seen_corrupt_file_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """A malformed JSON sidecar reads as the empty map, not a crash."""
    mem_fs[_CURE_SIDECAR_FILE] = "{not valid json"
    assert load_all_orphaned_cure_seen(_LEGACY_CURE_DIR) == {}


def test_load_all_orphaned_cure_seen_absent_file_is_empty() -> None:
    assert load_all_orphaned_cure_seen(Path("/fake/cure/none")) == {}


def test_orphaned_cure_adapter_seeds_from_legacy_json(
    mem_fs: dict[str, str],
) -> None:
    """Enabling the flag seeds the store once from the legacy JSON sidecar.

    The first ``prune_and_touch`` triggers the one-time seed; observing both
    seeded plan ids preserves their legacy first-seen iterations rather than
    restamping them at the current iteration.
    """
    mem_fs[_CURE_SIDECAR_FILE] = json.dumps({"ep1": 2, "ep2": 4})
    store = StateStore(":memory:")
    adapter = SqliteOrphanedCureStore(
        store, legacy_json_dir=_LEGACY_CURE_DIR,
    )
    assert adapter.prune_and_touch(["ep1", "ep2"], 10) == {"ep1": 2, "ep2": 4}
    assert store.is_orphaned_cure_seeded() is True


def test_orphaned_cure_adapter_load_triggers_seed(
    mem_fs: dict[str, str],
) -> None:
    """The read-only ``load`` also triggers the one-time seed on first access.

    The retirement-sweep consumer must see the seeded legacy map even if it reads
    before the rule's ``prune_and_touch`` runs this cycle.
    """
    mem_fs[_CURE_SIDECAR_FILE] = json.dumps({"ep1": 2})
    store = StateStore(":memory:")
    adapter = SqliteOrphanedCureStore(
        store, legacy_json_dir=_LEGACY_CURE_DIR,
    )
    assert adapter.load() == {"ep1": 2}
    assert store.is_orphaned_cure_seeded() is True


def test_orphaned_cure_adapter_durable_marker_blocks_reseed(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty with legacy present."""
    mem_fs[_CURE_SIDECAR_FILE] = json.dumps({"ep1": 2})
    store = StateStore(":memory:")
    store.seed_orphaned_cure_once({})  # empty seed stamps the marker
    adapter = SqliteOrphanedCureStore(
        store, legacy_json_dir=_LEGACY_CURE_DIR,
    )
    # The marker blocks the re-seed, so legacy {"ep1": 2} is not imported; a fresh
    # touch stamps "ep1" at the current iteration instead of the legacy 2.
    assert adapter.prune_and_touch(["ep1"], 8) == {"ep1": 8}
    assert store.is_orphaned_cure_seeded() is True


def test_orphaned_cure_adapter_no_seed_without_legacy_dir() -> None:
    adapter = SqliteOrphanedCureStore(
        StateStore(":memory:"), legacy_json_dir=None,
    )
    assert adapter.prune_and_touch(["ep_z"], 5) == {"ep_z": 5}


def test_orphaned_cure_adapter_no_seed_when_legacy_json_absent() -> None:
    adapter = SqliteOrphanedCureStore(
        StateStore(":memory:"),
        legacy_json_dir=Path("/fake/cure/missing"),
    )
    assert adapter.prune_and_touch(["ep_z"], 5) == {"ep_z": 5}


# ---------------------------------------------------------------------------
# Impact-review surfaced-set sidecar (proposal 106 Phase B Step 5)
# ---------------------------------------------------------------------------

_LEGACY_IMPACT_DIR = Path("/fake/impact-store")
_IMPACT_SIDECAR_FILE = str(
    _LEGACY_IMPACT_DIR / "impact_review_surfaced.json",
)


def test_impact_review_surfaced_store_record_and_load_round_trip() -> None:
    """``record`` unions ids idempotently; ``load`` returns the whole set."""
    store = StateStore(":memory:")
    assert store.load_impact_review_surfaced() == set()
    store.record_impact_review_surfaced(["v1", "v2"])
    assert store.load_impact_review_surfaced() == {"v1", "v2"}
    # Re-recording an existing id alongside a new one is idempotent on the
    # PRIMARY KEY — the duplicate is a silent no-op, the new id is added.
    store.record_impact_review_surfaced(["v2", "v3"])
    assert store.load_impact_review_surfaced() == {"v1", "v2", "v3"}
    # An empty record is a no-op.
    store.record_impact_review_surfaced([])
    assert store.load_impact_review_surfaced() == {"v1", "v2", "v3"}


def test_seed_impact_review_surfaced_once_is_idempotent() -> None:
    """The one-time seed stamps a durable marker; a second seed is a no-op."""
    store = StateStore(":memory:")
    store.seed_impact_review_surfaced_once({"v1"})
    assert store.load_impact_review_surfaced() == {"v1"}
    assert store.is_impact_review_surfaced_seeded() is True
    # A second seed (even with different data) is blocked by the marker.
    store.seed_impact_review_surfaced_once({"v2"})
    assert store.load_impact_review_surfaced() == {"v1"}


def test_impact_review_surfaced_json_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same record/load sequence yields identical sets on both backends.

    A fresh record, an idempotent re-record of an existing id alongside a new
    one, and the read-only ``load`` must all agree between the JSON sidecar and
    the SQLite keyed set table — the core proposal-106 parity guarantee.
    """
    json_store = JsonImpactReviewSurfacedStore(_LEGACY_IMPACT_DIR)
    sqlite_store = SqliteImpactReviewSurfacedStore(StateStore(":memory:"))

    # Both start empty.
    assert json_store.load() == set()
    assert sqlite_store.load() == set()

    # Fresh record unions two ids on both substrates.
    json_store.record(["v1", "v2"])
    sqlite_store.record(["v1", "v2"])
    assert json_store.load() == {"v1", "v2"}
    assert sqlite_store.load() == {"v1", "v2"}

    # Re-recording an existing id with a new one is idempotent — no duplicate,
    # the new id lands, on both substrates.
    json_store.record(["v2", "v3"])
    sqlite_store.record(["v2", "v3"])
    assert json_store.load() == {"v1", "v2", "v3"}
    assert sqlite_store.load() == {"v1", "v2", "v3"}


def test_load_all_impact_review_surfaced_parses_and_filters_junk(
    mem_fs: dict[str, str],
) -> None:
    """The seed source returns the surfaced set, dropping non-string members."""
    mem_fs[_IMPACT_SIDECAR_FILE] = json.dumps(["v1", "v2", 3, None])
    assert load_all_impact_review_surfaced(_LEGACY_IMPACT_DIR) == {"v1", "v2"}


def test_load_all_impact_review_surfaced_corrupt_file_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """A malformed JSON sidecar reads as the empty set, not a crash."""
    mem_fs[_IMPACT_SIDECAR_FILE] = "{not valid json"
    assert load_all_impact_review_surfaced(_LEGACY_IMPACT_DIR) == set()


def test_load_all_impact_review_surfaced_non_list_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """A sidecar holding valid non-list JSON reads as the empty set."""
    mem_fs[_IMPACT_SIDECAR_FILE] = json.dumps({"unexpected": "object"})
    assert load_all_impact_review_surfaced(_LEGACY_IMPACT_DIR) == set()


def test_load_all_impact_review_surfaced_absent_file_is_empty() -> None:
    assert load_all_impact_review_surfaced(Path("/fake/impact/none")) == set()


def test_impact_review_adapter_seeds_from_legacy_json(
    mem_fs: dict[str, str],
) -> None:
    """Enabling the flag seeds the store once from the legacy JSON sidecar.

    The first access (a ``load``) triggers the one-time seed, importing the
    already-surfaced ids so a version that surfaced under the JSON backend is not
    re-raised after the flip.
    """
    mem_fs[_IMPACT_SIDECAR_FILE] = json.dumps(["v1", "v2"])
    store = StateStore(":memory:")
    adapter = SqliteImpactReviewSurfacedStore(
        store, legacy_json_dir=_LEGACY_IMPACT_DIR,
    )
    assert adapter.load() == {"v1", "v2"}
    assert store.is_impact_review_surfaced_seeded() is True


def test_impact_review_adapter_record_triggers_seed(
    mem_fs: dict[str, str],
) -> None:
    """The ``record`` writer also triggers the one-time seed on first access."""
    mem_fs[_IMPACT_SIDECAR_FILE] = json.dumps(["v1"])
    store = StateStore(":memory:")
    adapter = SqliteImpactReviewSurfacedStore(
        store, legacy_json_dir=_LEGACY_IMPACT_DIR,
    )
    adapter.record(["v2"])
    assert adapter.load() == {"v1", "v2"}
    assert store.is_impact_review_surfaced_seeded() is True


def test_impact_review_adapter_durable_marker_blocks_reseed(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even with legacy present."""
    mem_fs[_IMPACT_SIDECAR_FILE] = json.dumps(["v1"])
    store = StateStore(":memory:")
    store.seed_impact_review_surfaced_once(set())  # empty seed stamps the marker
    adapter = SqliteImpactReviewSurfacedStore(
        store, legacy_json_dir=_LEGACY_IMPACT_DIR,
    )
    # The marker blocks the re-seed, so legacy ["v1"] is not imported.
    assert adapter.load() == set()
    assert store.is_impact_review_surfaced_seeded() is True


def test_impact_review_adapter_no_seed_without_legacy_dir() -> None:
    adapter = SqliteImpactReviewSurfacedStore(
        StateStore(":memory:"), legacy_json_dir=None,
    )
    adapter.record(["v_z"])
    assert adapter.load() == {"v_z"}


def test_impact_review_adapter_no_seed_when_legacy_json_absent() -> None:
    adapter = SqliteImpactReviewSurfacedStore(
        StateStore(":memory:"),
        legacy_json_dir=Path("/fake/impact/missing"),
    )
    adapter.record(["v_z"])
    assert adapter.load() == {"v_z"}


# ---------------------------------------------------------------------------
# Mergeable-flip serviced-marker ledger (proposal 106 Phase B Step 5)
# ---------------------------------------------------------------------------

_LEGACY_MERGEABLE_DIR = Path("/fake/mergeable-store")
_MERGEABLE_SIDECAR_FILE = str(
    _LEGACY_MERGEABLE_DIR / "mergeable_merge_requests.json",
)


def test_mergeable_serviced_store_record_and_is_serviced_round_trip() -> None:
    """``record`` adds a (pr, sha) pair idempotently; ``is_serviced`` tests
    membership on the composite key."""
    store = StateStore(":memory:")
    assert store.is_merge_request_serviced(2647, "abc") is False
    store.record_merge_request_serviced(2647, "abc")
    assert store.is_merge_request_serviced(2647, "abc") is True
    # A different head on the same PR, and the same head on a different PR, are
    # both distinct composite keys — neither is serviced by the (2647, "abc") row.
    assert store.is_merge_request_serviced(2647, "def") is False
    assert store.is_merge_request_serviced(9999, "abc") is False
    # Re-recording an existing pair is a silent no-op on the PRIMARY KEY.
    store.record_merge_request_serviced(2647, "abc")
    assert store.is_merge_request_serviced(2647, "abc") is True


def test_seed_mergeable_serviced_once_is_idempotent() -> None:
    """The one-time seed stamps a durable marker; a second seed is a no-op."""
    store = StateStore(":memory:")
    store.seed_mergeable_serviced_once({2647: {"abc"}})
    assert store.is_merge_request_serviced(2647, "abc") is True
    assert store.is_mergeable_serviced_seeded() is True
    # A second seed (even with different data) is blocked by the marker.
    store.seed_mergeable_serviced_once({9999: {"zzz"}})
    assert store.is_merge_request_serviced(9999, "zzz") is False


def test_mergeable_serviced_json_and_sqlite_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same record/is_serviced sequence agrees on both backends.

    A fresh record, an idempotent re-record of an existing pair, a new head on the
    same PR, and the same head on a different PR must all agree between the JSON
    sidecar and the SQLite composite-key table — the core proposal-106 parity
    guarantee.
    """
    json_store = JsonMergeRequestServicedStore(_LEGACY_MERGEABLE_DIR)
    sqlite_store = SqliteMergeRequestServicedStore(StateStore(":memory:"))

    corpus = [(2647, "abc"), (2647, "def"), (9001, "abc")]
    for pr, sha in corpus:
        assert json_store.is_serviced(pr, sha) is False
        assert sqlite_store.is_serviced(pr, sha) is False

    # Fresh record, then an idempotent re-record of the same pair, on both.
    json_store.record_serviced(2647, "abc")
    sqlite_store.record_serviced(2647, "abc")
    json_store.record_serviced(2647, "abc")
    sqlite_store.record_serviced(2647, "abc")
    for pr, sha in corpus:
        expected = (pr, sha) == (2647, "abc")
        assert json_store.is_serviced(pr, sha) is expected
        assert sqlite_store.is_serviced(pr, sha) is expected

    # A new head on the same PR and the same head on another PR are distinct keys.
    json_store.record_serviced(2647, "def")
    sqlite_store.record_serviced(2647, "def")
    assert json_store.is_serviced(2647, "def") is True
    assert sqlite_store.is_serviced(2647, "def") is True
    assert json_store.is_serviced(9001, "abc") is False
    assert sqlite_store.is_serviced(9001, "abc") is False


def test_load_all_serviced_merge_requests_parses_and_filters_junk(
    mem_fs: dict[str, str],
) -> None:
    """The seed source parses the map, dropping a non-digit key, a non-list
    value, and non-string SHA members."""
    mem_fs[_MERGEABLE_SIDECAR_FILE] = json.dumps({
        "2647": ["abc", "def", 3, None],
        "notpr": ["x"],
        "9001": "notalist",
    })
    assert load_all_serviced_merge_requests(_LEGACY_MERGEABLE_DIR) == {
        2647: {"abc", "def"},
    }


def test_load_all_serviced_merge_requests_corrupt_file_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """A malformed JSON sidecar reads as the empty map, not a crash."""
    mem_fs[_MERGEABLE_SIDECAR_FILE] = "{not valid json"
    assert load_all_serviced_merge_requests(_LEGACY_MERGEABLE_DIR) == {}


def test_load_all_serviced_merge_requests_non_dict_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """A sidecar holding valid non-dict JSON reads as the empty map."""
    mem_fs[_MERGEABLE_SIDECAR_FILE] = json.dumps(["unexpected", "list"])
    assert load_all_serviced_merge_requests(_LEGACY_MERGEABLE_DIR) == {}


def test_load_all_serviced_merge_requests_absent_file_is_empty() -> None:
    assert load_all_serviced_merge_requests(Path("/fake/mergeable/none")) == {}


def test_mergeable_serviced_adapter_seeds_from_legacy_json(
    mem_fs: dict[str, str],
) -> None:
    """Enabling the flag seeds the store once from the legacy JSON sidecar.

    The first access (an ``is_serviced``) triggers the one-time seed, importing
    the already-serviced (pr, sha) pairs so a head serviced under the JSON backend
    is not re-knocked after the flip.
    """
    mem_fs[_MERGEABLE_SIDECAR_FILE] = json.dumps({"2647": ["abc", "def"]})
    store = StateStore(":memory:")
    adapter = SqliteMergeRequestServicedStore(
        store, legacy_json_dir=_LEGACY_MERGEABLE_DIR,
    )
    assert adapter.is_serviced(2647, "abc") is True
    # A second access is a fast-path early return (``_seed_checked``).
    assert adapter.is_serviced(2647, "def") is True
    assert store.is_mergeable_serviced_seeded() is True


def test_mergeable_serviced_adapter_record_triggers_seed(
    mem_fs: dict[str, str],
) -> None:
    """The ``record_serviced`` writer also triggers the one-time seed on first
    access."""
    mem_fs[_MERGEABLE_SIDECAR_FILE] = json.dumps({"2647": ["abc"]})
    store = StateStore(":memory:")
    adapter = SqliteMergeRequestServicedStore(
        store, legacy_json_dir=_LEGACY_MERGEABLE_DIR,
    )
    adapter.record_serviced(2647, "def")
    assert adapter.is_serviced(2647, "abc") is True
    assert adapter.is_serviced(2647, "def") is True
    assert store.is_mergeable_serviced_seeded() is True


def test_mergeable_serviced_adapter_durable_marker_blocks_reseed(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even with legacy present."""
    mem_fs[_MERGEABLE_SIDECAR_FILE] = json.dumps({"2647": ["abc"]})
    store = StateStore(":memory:")
    store.seed_mergeable_serviced_once({})  # empty seed stamps the marker
    adapter = SqliteMergeRequestServicedStore(
        store, legacy_json_dir=_LEGACY_MERGEABLE_DIR,
    )
    # The marker blocks the re-seed, so legacy {2647: {"abc"}} is not imported.
    assert adapter.is_serviced(2647, "abc") is False
    assert store.is_mergeable_serviced_seeded() is True


def test_mergeable_serviced_adapter_no_seed_without_legacy_dir() -> None:
    adapter = SqliteMergeRequestServicedStore(
        StateStore(":memory:"), legacy_json_dir=None,
    )
    adapter.record_serviced(2647, "abc")
    assert adapter.is_serviced(2647, "abc") is True


def test_mergeable_serviced_adapter_no_seed_when_legacy_json_absent() -> None:
    adapter = SqliteMergeRequestServicedStore(
        StateStore(":memory:"),
        legacy_json_dir=Path("/fake/mergeable/missing"),
    )
    adapter.record_serviced(2647, "abc")
    assert adapter.is_serviced(2647, "abc") is True


# ---------------------------------------------------------------------------
# StateStore — settlement-reconciliation ledger (proposal 106)
# ---------------------------------------------------------------------------


def _recon(
    *,
    pr_number: int = 1,
    reconciler: str = "self_model",
    cleared: bool = True,
    detail: str | None = "note",
    at_iteration: int = 1,
) -> SettlementReconciliationRecord:
    return SettlementReconciliationRecord(
        pr_number=pr_number,
        reconciler=reconciler,
        cleared=cleared,
        detail=detail,
        at_iteration=at_iteration,
    )


def test_append_and_load_settlement_reconciliations_round_trip() -> None:
    store = StateStore(":memory:")
    r1 = _recon(
        pr_number=1, reconciler="escalation_standdown", cleared=True,
        detail="removed escalation standdown", at_iteration=1,
    )
    r2 = _recon(
        pr_number=2, reconciler="pr_ledger", cleared=False,
        detail=None, at_iteration=2,
    )
    store.append_settlement_reconciliation(r1)
    store.append_settlement_reconciliation(r2)
    # Oldest-first (id ASC); the ``cleared`` 0/1 and a NULL ``detail`` both
    # round-trip faithfully.
    assert store.load_settlement_reconciliations() == [r1, r2]


def test_settlement_reconciliations_seed_marker_defaults_false_then_stamps() -> None:
    """An empty seed stamps the durable marker and inserts no row."""
    store = StateStore(":memory:")
    assert store.is_settlement_reconciliations_seeded() is False
    store.seed_settlement_reconciliations_once([])
    assert store.is_settlement_reconciliations_seeded() is True
    assert store.load_settlement_reconciliations() == []


def test_seed_settlement_reconciliations_once_preserves_chronology() -> None:
    store = StateStore(":memory:")
    oldest_first = [
        _recon(pr_number=1, at_iteration=1),
        _recon(pr_number=2, at_iteration=2),
        _recon(pr_number=3, at_iteration=3),
    ]
    store.seed_settlement_reconciliations_once(oldest_first)
    got = store.load_settlement_reconciliations()
    assert [r.pr_number for r in got] == [1, 2, 3]
    assert store.is_settlement_reconciliations_seeded() is True


def test_seed_settlement_reconciliations_once_is_idempotent_and_never_doubles() -> None:
    """A second seed over the marked store re-checks the marker and no-ops."""
    store = StateStore(":memory:")
    legacy = [
        _recon(pr_number=1, at_iteration=1),
        _recon(pr_number=2, at_iteration=2),
    ]
    store.seed_settlement_reconciliations_once(legacy)
    # A second seed with DIFFERENT data inserts nothing (durable marker).
    store.seed_settlement_reconciliations_once(
        [_recon(pr_number=9, at_iteration=9)],
    )
    got = store.load_settlement_reconciliations()
    assert [r.pr_number for r in got] == [1, 2]


def test_seed_settlement_reconciliations_once_does_not_double_populated_unmarked() -> None:
    """A crash-populated-but-unmarked store is stamped, not re-inserted."""
    store = StateStore(":memory:")
    store.append_settlement_reconciliation(_recon(pr_number=9, at_iteration=9))
    store.seed_settlement_reconciliations_once(
        [_recon(pr_number=1, at_iteration=1)],
    )
    got = store.load_settlement_reconciliations()
    assert [r.pr_number for r in got] == [9]
    assert store.is_settlement_reconciliations_seeded() is True


def test_sqlite_settlement_reconciliation_adapter_appends_through_store() -> None:
    store = StateStore(":memory:")
    ledger = SqliteSettlementReconciliationLedger(store)
    row = _recon(
        pr_number=5, reconciler="stand_down_verdict", cleared=True,
        detail="released settled-PR stand-down verdict", at_iteration=4,
    )
    ledger.append(row)
    assert store.load_settlement_reconciliations() == [row]


def test_sqlite_settlement_reconciliation_adapter_seeds_from_legacy_jsonl(
    mem_fs: dict[str, str],
) -> None:
    """Enabling the flag seeds the store once from a legacy JSONL sidecar, so the
    operator-inspectable trace survives the flip; the new append lands after it."""
    data_dir = Path("/fake/recon-seed")
    jsonl = JsonlSettlementReconciliationLedger(data_dir)
    jsonl.append(_recon(pr_number=1, at_iteration=1))
    jsonl.append(_recon(pr_number=2, at_iteration=2))
    store = StateStore(":memory:")
    ledger = SqliteSettlementReconciliationLedger(
        store, legacy_jsonl_dir=data_dir,
    )
    ledger.append(_recon(pr_number=3, at_iteration=3))
    got = store.load_settlement_reconciliations()
    assert [r.pr_number for r in got] == [1, 2, 3]


def test_sqlite_settlement_reconciliation_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    """The seed fires at most once per instance: a legacy sidecar that grows after
    the first append is not re-read on the next."""
    data_dir = Path("/fake/recon-noreseed")
    jsonl = JsonlSettlementReconciliationLedger(data_dir)
    jsonl.append(_recon(pr_number=1, at_iteration=1))
    store = StateStore(":memory:")
    ledger = SqliteSettlementReconciliationLedger(
        store, legacy_jsonl_dir=data_dir,
    )
    ledger.append(_recon(pr_number=2, at_iteration=2))  # seeds {1}, then appends 2
    jsonl.append(_recon(pr_number=99, at_iteration=99))  # legacy grows post-seed
    ledger.append(_recon(pr_number=3, at_iteration=3))  # no re-seed
    got = store.load_settlement_reconciliations()
    assert [r.pr_number for r in got] == [1, 2, 3]


def test_sqlite_settlement_reconciliation_adapter_skips_seed_when_already_seeded(
    mem_fs: dict[str, str],
) -> None:
    """A store already carrying the durable marker is never re-seeded from the
    legacy JSONL — an aged-out trace stays put across the flip."""
    data_dir = Path("/fake/recon-preseeded")
    jsonl = JsonlSettlementReconciliationLedger(data_dir)
    jsonl.append(_recon(pr_number=1, at_iteration=1))
    store = StateStore(":memory:")
    store.seed_settlement_reconciliations_once([])  # already marked, empty
    ledger = SqliteSettlementReconciliationLedger(
        store, legacy_jsonl_dir=data_dir,
    )
    ledger.append(_recon(pr_number=2, at_iteration=2))
    got = store.load_settlement_reconciliations()
    assert [r.pr_number for r in got] == [2]


def test_sqlite_settlement_reconciliation_adapter_no_seed_without_legacy_dir() -> None:
    """With no legacy dir the adapter stamps the marker over an empty seed and
    reads no sidecar."""
    store = StateStore(":memory:")
    ledger = SqliteSettlementReconciliationLedger(store)
    ledger.append(_recon(pr_number=7, at_iteration=1))
    assert store.is_settlement_reconciliations_seeded() is True
    got = store.load_settlement_reconciliations()
    assert [r.pr_number for r in got] == [7]


def test_jsonl_and_sqlite_settlement_reconciliation_backends_are_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same append sequence yields identical rows through both backends."""
    corpus = [
        _recon(
            pr_number=1, reconciler="self_model", cleared=True,
            detail="x", at_iteration=1,
        ),
        _recon(
            pr_number=2, reconciler="pr_ledger", cleared=False,
            detail=None, at_iteration=2,
        ),
        _recon(
            pr_number=3, reconciler="stand_down_verdict", cleared=True,
            detail="y", at_iteration=3,
        ),
    ]
    jsonl = JsonlSettlementReconciliationLedger(Path("/fake/recon-parity"))
    sqlite = SqliteSettlementReconciliationLedger(StateStore(":memory:"))
    for row in corpus:
        jsonl.append(row)
        sqlite.append(row)
    assert load_all_settlement_reconciliations(Path("/fake/recon-parity")) == corpus
    assert sqlite._store.load_settlement_reconciliations() == corpus


# ---------------------------------------------------------------------------
# StateStore pr_ledger methods + SqlitePRLedger adapter (proposal 106)
# ---------------------------------------------------------------------------

_LEGACY_PR_LEDGER_DIR = Path("/fake/pr_ledger")
_PR_LEDGER_FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _pr_ledger_row_count(store: StateStore) -> int:
    return int(
        store._conn.execute("SELECT COUNT(*) FROM pr_ledger").fetchone()[0],
    )


def _seed_legacy_pr_ledger(*entries: PRLedgerEntry) -> None:
    """Write a legacy ``pr_ledger.json`` under mem_fs via the JSON adapter."""
    JsonPRLedger(_LEGACY_PR_LEDGER_DIR).mutate(
        lambda m: m.update({e.pr_number: e for e in entries}),
    )


def test_state_store_creates_pr_ledger_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='pr_ledger'",
    )
    assert cur.fetchone() is not None


def test_load_and_replace_pr_ledger_round_trip() -> None:
    store = StateStore(":memory:")
    store.replace_pr_ledger([
        PRLedgerEntry(pr_number=2, branch="b2", observed_head="h2"),
        PRLedgerEntry(pr_number=1, branch="b1", stuck=True, stuck_reason="x"),
    ])
    loaded = store.load_pr_ledger()
    assert set(loaded) == {1, 2}
    assert loaded[1].stuck is True
    assert loaded[1].stuck_reason == "x"
    assert loaded[2].observed_head == "h2"


def test_replace_pr_ledger_is_whole_map_replace() -> None:
    """A later replace drops entries absent from the new list (no orphan rows)."""
    store = StateStore(":memory:")
    store.replace_pr_ledger(
        [PRLedgerEntry(pr_number=1), PRLedgerEntry(pr_number=2)],
    )
    store.replace_pr_ledger([PRLedgerEntry(pr_number=1)])
    assert set(store.load_pr_ledger()) == {1}


def test_load_pr_ledger_tolerates_corrupt_blob() -> None:
    """A structurally-corrupt ``data`` blob is dropped, not raised."""
    store = StateStore(":memory:")
    store.replace_pr_ledger([PRLedgerEntry(pr_number=1, branch="ok")])
    store._conn.execute(
        "INSERT OR REPLACE INTO pr_ledger (pr_number, data) VALUES (?, ?)",
        (2, json.dumps({"no_pr_number": True})),
    )
    assert set(store.load_pr_ledger()) == {1}


def test_seed_pr_ledger_once_seeds_in_order() -> None:
    store = StateStore(":memory:")
    store.seed_pr_ledger_once([
        PRLedgerEntry(pr_number=1, branch="b1"),
        PRLedgerEntry(pr_number=2, branch="b2"),
    ])
    assert set(store.load_pr_ledger()) == {1, 2}
    assert _pr_ledger_row_count(store) == 2
    assert store.is_pr_ledger_seeded() is True


def test_seed_pr_ledger_once_empty_list_stamps_marker() -> None:
    store = StateStore(":memory:")
    assert store.is_pr_ledger_seeded() is False
    store.seed_pr_ledger_once([])
    assert store.is_pr_ledger_seeded() is True
    assert _pr_ledger_row_count(store) == 0


def test_seed_pr_ledger_once_is_idempotent_and_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = [PRLedgerEntry(pr_number=1), PRLedgerEntry(pr_number=2)]
    store.seed_pr_ledger_once(legacy)
    store.seed_pr_ledger_once(legacy)
    assert _pr_ledger_row_count(store) == 2


def test_seed_pr_ledger_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_pr_ledger_once([])  # empty seed stamps the marker
    store.seed_pr_ledger_once([PRLedgerEntry(pr_number=1)])
    assert _pr_ledger_row_count(store) == 0


def test_seed_pr_ledger_once_does_not_overwrite_populated_store() -> None:
    """A crash-populated-but-unmarked store keeps its live rows.

    The empty-table guard means a re-seed never clobbers a live entry (PR 1 at
    ``live``) with a stale legacy row (PR 1 at ``stale``).
    """
    store = StateStore(":memory:")
    store.replace_pr_ledger([PRLedgerEntry(pr_number=1, observed_head="live")])
    store.seed_pr_ledger_once(
        [PRLedgerEntry(pr_number=1, observed_head="stale")],
    )
    assert store.load_pr_ledger()[1].observed_head == "live"
    assert _pr_ledger_row_count(store) == 1
    assert store.is_pr_ledger_seeded() is True


def test_pr_ledger_adapter_seeds_from_legacy_json(mem_fs: dict[str, str]) -> None:
    _seed_legacy_pr_ledger(
        PRLedgerEntry(pr_number=1, branch="b1"),
        PRLedgerEntry(pr_number=2, branch="b2", stuck=True),
    )
    ledger = SqlitePRLedger(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_PR_LEDGER_DIR,
    )
    entries = ledger.read()  # first read triggers the one-time seed
    assert set(entries) == {1, 2}
    assert entries[2].stuck is True


def test_pr_ledger_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_pr_ledger(PRLedgerEntry(pr_number=1, branch="b1"))
    store = StateStore(":memory:")
    first = SqlitePRLedger(store, legacy_json_dir=_LEGACY_PR_LEDGER_DIR)
    assert set(first.read()) == {1}
    # A second adapter over the already-seeded store must not re-seed.
    second = SqlitePRLedger(store, legacy_json_dir=_LEGACY_PR_LEDGER_DIR)
    assert set(second.read()) == {1}
    assert _pr_ledger_row_count(store) == 1


def test_pr_ledger_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_pr_ledger(PRLedgerEntry(pr_number=1))
    ledger = SqlitePRLedger(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_PR_LEDGER_DIR,
    )
    assert set(ledger.read()) == {1}  # seeds + flips the guard
    assert set(ledger.read()) == {1}  # second access runs no seed check


def test_pr_ledger_adapter_no_seed_without_legacy_dir() -> None:
    ledger = SqlitePRLedger(StateStore(":memory:"), legacy_json_dir=None)
    assert ledger.read() == {}


def test_pr_ledger_adapter_no_seed_when_legacy_json_absent() -> None:
    ledger = SqlitePRLedger(
        StateStore(":memory:"),
        legacy_json_dir=Path("/fake/pr_ledger/missing"),
    )
    assert ledger.read() == {}


def test_pr_ledger_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty with legacy present."""
    _seed_legacy_pr_ledger(PRLedgerEntry(pr_number=1))
    store = StateStore(":memory:")
    store.seed_pr_ledger_once([])  # empty seed stamps the marker
    ledger = SqlitePRLedger(store, legacy_json_dir=_LEGACY_PR_LEDGER_DIR)
    assert ledger.read() == {}  # marker blocks the re-seed
    assert store.is_pr_ledger_seeded() is True


def test_pr_ledger_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    """A live store carrying newer state is not clobbered by the frozen legacy."""
    _seed_legacy_pr_ledger(PRLedgerEntry(pr_number=1, observed_head="stale"))
    store = StateStore(":memory:")
    store.replace_pr_ledger([PRLedgerEntry(pr_number=1, observed_head="live")])
    ledger = SqlitePRLedger(store, legacy_json_dir=_LEGACY_PR_LEDGER_DIR)
    assert ledger.read()[1].observed_head == "live"


def test_pr_ledger_adapter_mutate_persists_and_change_gates(
    mem_fs: dict[str, str],
) -> None:
    store = StateStore(":memory:")
    ledger = SqlitePRLedger(store)

    def _add(entries: dict[int, PRLedgerEntry]) -> int:
        entries[9] = PRLedgerEntry(pr_number=9, branch="b9")
        return len(entries)

    assert ledger.mutate(_add) == 1
    assert set(store.load_pr_ledger()) == {9}
    # A no-op closure writes no row (change-gated).
    assert ledger.mutate(lambda m: "noop") == "noop"
    assert _pr_ledger_row_count(store) == 1


def test_mutate_pr_ledger_runs_rmw_inside_one_write_transaction() -> None:
    """The load → *fn* → replace runs inside one ``BEGIN IMMEDIATE`` write
    transaction, so a concurrent ``state.db`` connection cannot interleave
    between the load and the persist — the atomicity the JSON store's
    ``file_lock`` span provides."""
    store = StateStore(":memory:")
    in_transaction_during_fn: list[bool] = []

    def _add(entries: dict[int, PRLedgerEntry]) -> None:
        in_transaction_during_fn.append(store._conn.in_transaction)
        entries[1] = PRLedgerEntry(pr_number=1, branch="b1")

    store.mutate_pr_ledger(_add)
    assert in_transaction_during_fn == [True]
    assert store._conn.in_transaction is False
    assert set(store.load_pr_ledger()) == {1}


def test_mutate_pr_ledger_rolls_back_when_fn_raises() -> None:
    """A raising *fn* re-raises, persists nothing, and leaves no transaction
    open — the guard branch of the one-transaction RMW."""
    store = StateStore(":memory:")
    store.replace_pr_ledger([PRLedgerEntry(pr_number=1, branch="keep")])

    def _boom(entries: dict[int, PRLedgerEntry]) -> None:
        entries[2] = PRLedgerEntry(pr_number=2)
        raise RuntimeError("mutation failed")

    with pytest.raises(RuntimeError, match="mutation failed"):
        store.mutate_pr_ledger(_boom)
    assert store._conn.in_transaction is False
    assert set(store.load_pr_ledger()) == {1}


def test_pr_ledger_json_and_sqlite_backends_are_object_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same op sequence yields identical reads through both backends.

    Drives ``observe_head`` → ``record_attempt`` (to the stuck latch) →
    ``grant_fresh_attempts`` → ``mark_escalated`` → ``prune`` → ``evict_settled``
    through a JSON-backed and a SQLite-backed ``PRLedger``, asserting ``get`` /
    ``can_attempt`` / ``stuck_entries`` / ``parked_entries`` / ``known_branches``
    agree after every step. The SQLite side uses ``StateStore(":memory:")``; only
    the JSON side touches ``mem_fs``. A fixed ``now`` keeps the recorded
    timestamps identical across substrates.
    """
    json_ledger = PRLedger(
        Path("/fake/parity"), max_attempts_per_head=2,
        backend=JsonPRLedger(Path("/fake/parity")),
    )
    sqlite_ledger = PRLedger(
        Path("/unused"), max_attempts_per_head=2,
        backend=SqlitePRLedger(StateStore(":memory:")),
    )
    both = (json_ledger, sqlite_ledger)

    def _assert_parity() -> None:
        for pr in (1, 2):
            assert json_ledger.get(pr) == sqlite_ledger.get(pr)
            assert json_ledger.can_attempt(pr) == sqlite_ledger.can_attempt(pr)
        assert json_ledger.stuck_entries() == sqlite_ledger.stuck_entries()
        assert json_ledger.parked_entries() == sqlite_ledger.parked_entries()
        assert json_ledger.known_branches() == sqlite_ledger.known_branches()

    for ledger in both:
        ledger.observe_head(1, "b1", "sha1")
        ledger.observe_head(2, "b2", "sha2")
    _assert_parity()

    for ledger in both:
        ledger.record_attempt(
            1, outcome="noop", stuck_reason="blocked", now=_PR_LEDGER_FIXED_NOW,
        )
        ledger.record_attempt(
            1, outcome="noop", stuck_reason="blocked", now=_PR_LEDGER_FIXED_NOW,
        )
    _assert_parity()  # PR 1 now latched stuck

    for ledger in both:
        ledger.grant_fresh_attempts(1, reason="new conflict")
    _assert_parity()

    for ledger in both:
        ledger.record_attempt(
            1, outcome="noop", stuck_reason="blocked", now=_PR_LEDGER_FIXED_NOW,
        )
        ledger.record_attempt(
            1, outcome="noop", stuck_reason="blocked", now=_PR_LEDGER_FIXED_NOW,
        )
        ledger.mark_escalated(1, now=_PR_LEDGER_FIXED_NOW)
    _assert_parity()

    for ledger in both:
        assert ledger.prune({1}) == [2]
    _assert_parity()

    for ledger in both:
        assert ledger.evict_settled(1) is True
    _assert_parity()


# ---------------------------------------------------------------------------
# StateStore remediation-tracker methods (proposal 106)
# ---------------------------------------------------------------------------

_LEGACY_TRACKER_DIR = Path("/fake/tracker")


def _tracker_row_count(store: StateStore) -> int:
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM remediation_tracker",
        ).fetchone()[0],
    )


def test_state_store_creates_remediation_tracker_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='remediation_tracker'",
    )
    assert cur.fetchone() is not None


def test_load_remediation_tracker_is_empty_and_version_zero_when_unwritten() -> None:
    # Never persisted or seeded reads as a fresh tracker, so the one-time
    # merge-block reconciliation is not owed on a brand-new store.
    store = StateStore(":memory:")
    assert store.load_remediation_tracker() == TrackerState({}, {}, {}, {}, 0)


def test_replace_and_load_remediation_tracker_round_trip() -> None:
    store = StateStore(":memory:")
    state = TrackerState(
        {"42": [True, False], "43": [True]},
        {"42": 3},
        {"43": 2},
        {"44": 1},
        2,
    )
    store.replace_remediation_tracker(state)
    assert store.load_remediation_tracker() == state
    # One row per PR key present in any facet — 42, 43, 44.
    assert _tracker_row_count(store) == 3


def test_replace_remediation_tracker_round_trips_empty_but_present_facets() -> None:
    # A PR present only in ``skips`` keeps exactly that facet; it does not
    # acquire an empty outcome list on the way back.
    store = StateStore(":memory:")
    store.replace_remediation_tracker(TrackerState({}, {}, {"9": 4}, {}, 2))
    loaded = store.load_remediation_tracker()
    assert loaded == TrackerState({}, {}, {"9": 4}, {}, 2)


def test_replace_remediation_tracker_is_whole_map_replace() -> None:
    """A later replace drops keys absent from the new state (no orphan rows)."""
    store = StateStore(":memory:")
    store.replace_remediation_tracker(
        TrackerState({"1": [True], "2": [False]}, {}, {}, {}, 2),
    )
    store.replace_remediation_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    assert store.load_remediation_tracker().outcomes == {"1": [True]}
    assert _tracker_row_count(store) == 1


def test_replace_remediation_tracker_upserts_the_version() -> None:
    # The version is state-wide metadata, so it survives a replace that empties
    # the table — and a later replace moves it.
    store = StateStore(":memory:")
    store.replace_remediation_tracker(TrackerState({"1": [True]}, {}, {}, {}, 1))
    assert store.load_remediation_tracker().schema_version == 1
    store.replace_remediation_tracker(TrackerState({}, {}, {}, {}, 2))
    assert store.load_remediation_tracker() == TrackerState({}, {}, {}, {}, 2)


def test_load_remediation_tracker_tolerates_corrupt_blob() -> None:
    """A non-mapping blob contributes nothing; a wrong-shaped facet is dropped."""
    store = StateStore(":memory:")
    store.replace_remediation_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    store._conn.execute(
        "INSERT OR REPLACE INTO remediation_tracker (pr_key, data) "
        "VALUES (?, ?)",
        ("2", json.dumps(["not", "a", "mapping"])),
    )
    store._conn.execute(
        "INSERT OR REPLACE INTO remediation_tracker (pr_key, data) "
        "VALUES (?, ?)",
        ("3", json.dumps({"outcomes": "not-a-list", "skips": True})),
    )
    # Only the well-formed row survives; ``bool`` never poses as a skip count.
    assert store.load_remediation_tracker() == TrackerState(
        {"1": [True]}, {}, {}, {}, 2,
    )


@pytest.mark.parametrize("tampered", ["not-a-number", "--5", "²"])
def test_load_remediation_tracker_tolerates_non_integer_version(
    tampered: str,
) -> None:
    """A tampered version stamp reads as the legacy version, as the JSON path does.

    ``"--5"`` and ``"²"`` pass an ``isdigit``-style pre-check yet ``int()``
    rejects them — they must degrade like any other tampered stamp, never raise.
    """
    store = StateStore(":memory:")
    store.replace_remediation_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    store._conn.execute(
        "INSERT OR REPLACE INTO store_metadata (key, value) VALUES (?, ?)",
        ("remediation_tracker_schema_version", tampered),
    )
    assert store.load_remediation_tracker().schema_version == 1


def test_load_remediation_tracker_reads_a_negative_version() -> None:
    # The numeric guard accepts a signed integer rather than falling back.
    store = StateStore(":memory:")
    store.replace_remediation_tracker(TrackerState({}, {}, {}, {}, -3))
    assert store.load_remediation_tracker().schema_version == -3


def test_seed_remediation_tracker_once_seeds_in_order() -> None:
    store = StateStore(":memory:")
    store.seed_remediation_tracker_once(
        TrackerState({"2": [True], "1": [False]}, {"1": 5}, {}, {}, 2),
    )
    loaded = store.load_remediation_tracker()
    assert loaded.outcomes == {"1": [False], "2": [True]}
    assert loaded.items_remaining == {"1": 5}
    assert _tracker_row_count(store) == 2
    assert store.is_remediation_tracker_seeded() is True
    # Rows land in sorted key order for a deterministic on-disk layout.
    keys = [
        row[0] for row in store._conn.execute(
            "SELECT pr_key FROM remediation_tracker ORDER BY rowid",
        ).fetchall()
    ]
    assert keys == ["1", "2"]


def test_seed_remediation_tracker_once_empty_state_stamps_marker() -> None:
    store = StateStore(":memory:")
    assert store.is_remediation_tracker_seeded() is False
    store.seed_remediation_tracker_once(TrackerState({}, {}, {}, {}, 0))
    assert store.is_remediation_tracker_seeded() is True
    assert _tracker_row_count(store) == 0


def test_seed_remediation_tracker_once_is_idempotent_and_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = TrackerState({"1": [True], "2": [False]}, {}, {}, {}, 2)
    store.seed_remediation_tracker_once(legacy)
    store.seed_remediation_tracker_once(legacy)
    assert _tracker_row_count(store) == 2


def test_seed_remediation_tracker_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_remediation_tracker_once(TrackerState({}, {}, {}, {}, 0))
    store.seed_remediation_tracker_once(TrackerState({"1": [True]}, {}, {}, {}, 2))
    assert _tracker_row_count(store) == 0


def test_seed_remediation_tracker_once_does_not_overwrite_populated_store() -> None:
    """A crash-populated-but-unmarked store keeps its live rows and version."""
    store = StateStore(":memory:")
    store.replace_remediation_tracker(
        TrackerState({"1": [True, True]}, {}, {}, {}, 2),
    )
    store.seed_remediation_tracker_once(TrackerState({"1": [False]}, {}, {}, {}, 1))
    loaded = store.load_remediation_tracker()
    assert loaded.outcomes == {"1": [True, True]}
    assert loaded.schema_version == 2
    assert store.is_remediation_tracker_seeded() is True


def test_seed_remediation_tracker_once_preserves_a_legacy_version() -> None:
    """A pre-v2 legacy state keeps its stamp, so the migration still fires once."""
    store = StateStore(":memory:")
    store.seed_remediation_tracker_once(
        TrackerState({"1": [True]}, {}, {}, {}, 1),
    )
    assert store.load_remediation_tracker().schema_version == 1


# ---------------------------------------------------------------------------
# SqliteRemediationTrackerStore adapter (proposal 106)
# ---------------------------------------------------------------------------


def _seed_legacy_tracker(state: TrackerState) -> None:
    """Write a legacy ``remediation_tracker.json`` under mem_fs via the adapter."""
    JsonRemediationTrackerStore(_LEGACY_TRACKER_DIR).persist(state)


def test_remediation_tracker_adapters_are_runtime_checkable(
    mem_fs: dict[str, str],
) -> None:
    # ``@runtime_checkable`` lets the agent's backend-selection tests assert
    # structural conformance for either substrate.
    assert isinstance(
        JsonRemediationTrackerStore(_LEGACY_TRACKER_DIR),
        RemediationTrackerBackend,
    )
    assert isinstance(
        SqliteRemediationTrackerStore(StateStore(":memory:")),
        RemediationTrackerBackend,
    )


def test_tracker_adapter_seeds_from_legacy_json(mem_fs: dict[str, str]) -> None:
    _seed_legacy_tracker(
        TrackerState({"1": [True]}, {"1": 4}, {"2": 3}, {"3": 2}, 2),
    )
    adapter = SqliteRemediationTrackerStore(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    loaded = adapter.load()  # first load triggers the one-time seed
    assert loaded == TrackerState({"1": [True]}, {"1": 4}, {"2": 3}, {"3": 2}, 2)


def test_tracker_adapter_seed_preserves_a_pre_v2_version(
    mem_fs: dict[str, str],
) -> None:
    # A pre-#2796 sidecar carries version 1 across the flip, so the one-time
    # merge-block reconciliation is still owed on the SQLite substrate.
    _seed_legacy_tracker(TrackerState({"1": [True]}, {}, {}, {}, 1))
    adapter = SqliteRemediationTrackerStore(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    assert adapter.load().schema_version == 1


def test_tracker_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    store = StateStore(":memory:")
    first = SqliteRemediationTrackerStore(
        store, legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    assert first.load().outcomes == {"1": [True]}
    # A second adapter over the already-seeded store must not re-seed.
    second = SqliteRemediationTrackerStore(
        store, legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    assert second.load().outcomes == {"1": [True]}
    assert _tracker_row_count(store) == 1


def test_tracker_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    adapter = SqliteRemediationTrackerStore(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    assert adapter.load().outcomes == {"1": [True]}  # seeds + flips the guard
    assert adapter.load().outcomes == {"1": [True]}  # no second seed check


def test_tracker_adapter_no_seed_without_legacy_dir() -> None:
    adapter = SqliteRemediationTrackerStore(
        StateStore(":memory:"), legacy_json_dir=None,
    )
    assert adapter.load() == TrackerState({}, {}, {}, {}, 0)


def test_tracker_adapter_no_seed_when_legacy_json_absent() -> None:
    adapter = SqliteRemediationTrackerStore(
        StateStore(":memory:"),
        legacy_json_dir=Path("/fake/tracker/missing"),
    )
    assert adapter.load() == TrackerState({}, {}, {}, {}, 0)


def test_tracker_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    """A durably-marked store is never re-seeded, even if empty with legacy present."""
    _seed_legacy_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    store = StateStore(":memory:")
    store.seed_remediation_tracker_once(TrackerState({}, {}, {}, {}, 0))
    adapter = SqliteRemediationTrackerStore(
        store, legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    assert adapter.load() == TrackerState({}, {}, {}, {}, 0)
    assert store.is_remediation_tracker_seeded() is True


def test_tracker_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    """A live store carrying newer gate state is not clobbered by the frozen legacy."""
    _seed_legacy_tracker(TrackerState({"1": [False]}, {}, {}, {}, 2))
    store = StateStore(":memory:")
    store.replace_remediation_tracker(
        TrackerState({"1": [True, True]}, {}, {}, {}, 2),
    )
    adapter = SqliteRemediationTrackerStore(
        store, legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    assert adapter.load().outcomes == {"1": [True, True]}


def test_tracker_adapter_persist_replaces_and_leaves_legacy_json_untouched(
    mem_fs: dict[str, str],
) -> None:
    # ``persist`` is a whole-state replace on the store; the legacy sidecar is
    # read once for the seed and never written — it stays the rollback.
    _seed_legacy_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    legacy_path = str(_LEGACY_TRACKER_DIR / "remediation_tracker.json")
    before = mem_fs[legacy_path]
    store = StateStore(":memory:")
    adapter = SqliteRemediationTrackerStore(
        store, legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    adapter.persist(TrackerState({"5": [False]}, {"5": 1}, {}, {}, 2))
    assert adapter.load() == TrackerState({"5": [False]}, {"5": 1}, {}, {}, 2)
    assert mem_fs[legacy_path] == before


def test_tracker_adapter_persist_seeds_before_first_write(
    mem_fs: dict[str, str],
) -> None:
    """A first access through ``persist`` still stamps the one-time seed marker."""
    _seed_legacy_tracker(TrackerState({"1": [True]}, {}, {}, {}, 2))
    store = StateStore(":memory:")
    adapter = SqliteRemediationTrackerStore(
        store, legacy_json_dir=_LEGACY_TRACKER_DIR,
    )
    adapter.persist(TrackerState({"9": [True]}, {}, {}, {}, 2))
    assert store.is_remediation_tracker_seeded() is True
    # The write wins over the seed it followed — no legacy residue.
    assert store.load_remediation_tracker().outcomes == {"9": [True]}


# ---------------------------------------------------------------------------
# StateStore escalation-standdown ledger methods + SqliteEscalationLedger
# adapter (proposal 106). All on ``mem_fs`` / ``StateStore(":memory:")``.
# ---------------------------------------------------------------------------

_LEGACY_ESCALATION_DIR = Path("/fake/escalation")


def _escalation_row_count(store: StateStore) -> int:
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM escalation_standdowns",
        ).fetchone()[0],
    )


def _record(head_sha: str = "h", serviced: list[int] | None = None) -> StanddownRecord:
    """Build a minimal canonical standdown record for direct store writes."""
    return {
        "head_sha": head_sha,
        "delivered_at": "2026-01-01T00:00:00+00:00",
        "serviced_comment_ids": serviced if serviced is not None else [],
    }


def _seed_legacy_escalation(raw: dict[str, object]) -> None:
    """Write a legacy ``escalation_handoffs.json`` under mem_fs (raw shape)."""
    from maestro.fileutil import atomic_write
    atomic_write(
        _LEGACY_ESCALATION_DIR / "escalation_handoffs.json", json.dumps(raw),
    )


def test_state_store_creates_escalation_standdowns_table() -> None:
    store = StateStore(":memory:")
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='escalation_standdowns'",
    )
    assert cur.fetchone() is not None


def test_load_escalation_standdowns_is_empty_when_unwritten() -> None:
    assert StateStore(":memory:").load_escalation_standdowns() == {}


def test_mutate_and_load_escalation_standdowns_round_trip() -> None:
    store = StateStore(":memory:")
    store.mutate_escalation_standdowns(
        lambda m: m.update({1: _record("h1"), 2: _record("h2", [7])}),
    )
    loaded = store.load_escalation_standdowns()
    assert set(loaded) == {1, 2}
    assert loaded[2]["serviced_comment_ids"] == [7]


def test_write_escalation_standdown_rows_is_whole_map_replace() -> None:
    """A later mutate that drops a key leaves no orphan row."""
    store = StateStore(":memory:")
    store.mutate_escalation_standdowns(
        lambda m: m.update({1: _record(), 2: _record()}),
    )
    store.mutate_escalation_standdowns(lambda m: m.pop(2))
    assert set(store.load_escalation_standdowns()) == {1}


def test_load_escalation_standdowns_tolerates_corrupt_blob() -> None:
    """A structurally-corrupt ``data`` blob is dropped, not raised."""
    store = StateStore(":memory:")
    store.mutate_escalation_standdowns(lambda m: m.update({1: _record("ok")}))
    store._conn.execute(
        "INSERT OR REPLACE INTO escalation_standdowns (number, data) "
        "VALUES (?, ?)",
        (2, json.dumps({"no_head_sha": True})),
    )
    assert set(store.load_escalation_standdowns()) == {1}


def test_mutate_escalation_standdowns_noop_writes_no_row() -> None:
    store = StateStore(":memory:")
    assert store.mutate_escalation_standdowns(lambda m: "noop") == "noop"
    assert _escalation_row_count(store) == 0


def test_seed_escalation_standdowns_once_seeds_in_order() -> None:
    store = StateStore(":memory:")
    store.seed_escalation_standdowns_once({1: _record("h1"), 2: _record("h2")})
    assert set(store.load_escalation_standdowns()) == {1, 2}
    assert _escalation_row_count(store) == 2
    assert store.is_escalation_standdowns_seeded() is True


def test_seed_escalation_standdowns_once_empty_map_stamps_marker() -> None:
    store = StateStore(":memory:")
    assert store.is_escalation_standdowns_seeded() is False
    store.seed_escalation_standdowns_once({})
    assert store.is_escalation_standdowns_seeded() is True
    assert _escalation_row_count(store) == 0


def test_seed_escalation_standdowns_once_is_idempotent_and_never_doubles() -> None:
    store = StateStore(":memory:")
    legacy = {1: _record(), 2: _record()}
    store.seed_escalation_standdowns_once(legacy)
    store.seed_escalation_standdowns_once(legacy)
    assert _escalation_row_count(store) == 2


def test_seed_escalation_standdowns_once_skips_when_already_marked() -> None:
    """A durably-marked store never seeds, even while its table is empty."""
    store = StateStore(":memory:")
    store.seed_escalation_standdowns_once({})  # empty seed stamps the marker
    store.seed_escalation_standdowns_once({1: _record()})
    assert _escalation_row_count(store) == 0


def test_seed_escalation_standdowns_once_does_not_overwrite_populated_store() -> None:
    """A crash-populated-but-unmarked store keeps its live rows."""
    store = StateStore(":memory:")
    store.mutate_escalation_standdowns(lambda m: m.update({1: _record("live")}))
    store.seed_escalation_standdowns_once({1: _record("stale")})
    assert store.load_escalation_standdowns()[1]["head_sha"] == "live"
    assert _escalation_row_count(store) == 1
    assert store.is_escalation_standdowns_seeded() is True


def test_escalation_adapter_seeds_from_legacy_json(mem_fs: dict[str, str]) -> None:
    """The adapter seeds once from the legacy JSON, migrating a pre-080
    ``"<pr>@<head>"`` key to a canonical ``number`` key at seed time."""
    _seed_legacy_escalation({
        "1": {"head_sha": "h1", "delivered_at": "t", "serviced_comment_ids": []},
        "2@abc12345": "2026-01-01T00:00:00+00:00",
    })
    adapter = SqliteEscalationLedger(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_ESCALATION_DIR,
    )
    entries = adapter.read()  # first read triggers the one-time seed
    assert set(entries) == {1, 2}
    assert entries[2]["head_sha"] == "abc12345"


def test_escalation_adapter_seed_is_one_time_idempotent(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_escalation({"1": {
        "head_sha": "h1", "delivered_at": "t", "serviced_comment_ids": [],
    }})
    store = StateStore(":memory:")
    first = SqliteEscalationLedger(store, legacy_json_dir=_LEGACY_ESCALATION_DIR)
    assert set(first.read()) == {1}
    second = SqliteEscalationLedger(store, legacy_json_dir=_LEGACY_ESCALATION_DIR)
    assert set(second.read()) == {1}
    assert _escalation_row_count(store) == 1


def test_escalation_adapter_does_not_reseed_after_seed_checked(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_escalation({"1": {
        "head_sha": "h1", "delivered_at": "t", "serviced_comment_ids": [],
    }})
    adapter = SqliteEscalationLedger(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_ESCALATION_DIR,
    )
    assert set(adapter.read()) == {1}  # seeds + flips the guard
    assert set(adapter.read()) == {1}  # second access runs no seed check


def test_escalation_adapter_no_seed_without_legacy_dir() -> None:
    adapter = SqliteEscalationLedger(StateStore(":memory:"), legacy_json_dir=None)
    assert adapter.read() == {}


def test_escalation_adapter_no_seed_when_legacy_json_absent() -> None:
    adapter = SqliteEscalationLedger(
        StateStore(":memory:"),
        legacy_json_dir=Path("/fake/escalation/missing"),
    )
    assert adapter.read() == {}


def test_escalation_adapter_durable_marker_blocks_reseed_over_empty_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_escalation({"1": {
        "head_sha": "h1", "delivered_at": "t", "serviced_comment_ids": [],
    }})
    store = StateStore(":memory:")
    store.seed_escalation_standdowns_once({})  # empty seed stamps the marker
    adapter = SqliteEscalationLedger(store, legacy_json_dir=_LEGACY_ESCALATION_DIR)
    assert adapter.read() == {}  # marker blocks the re-seed
    assert store.is_escalation_standdowns_seeded() is True


def test_escalation_adapter_does_not_seed_over_populated_store(
    mem_fs: dict[str, str],
) -> None:
    _seed_legacy_escalation({"1": {
        "head_sha": "stale", "delivered_at": "t", "serviced_comment_ids": [],
    }})
    store = StateStore(":memory:")
    store.mutate_escalation_standdowns(lambda m: m.update({1: _record("live")}))
    adapter = SqliteEscalationLedger(store, legacy_json_dir=_LEGACY_ESCALATION_DIR)
    assert adapter.read()[1]["head_sha"] == "live"


def test_escalation_adapter_mutate_persists_and_change_gates(
    mem_fs: dict[str, str],
) -> None:
    store = StateStore(":memory:")
    adapter = SqliteEscalationLedger(store)
    assert adapter.mutate(lambda m: m.update({9: _record("b9")}) or len(m)) == 1
    assert set(store.load_escalation_standdowns()) == {9}
    # A no-op closure writes no row (change-gated).
    assert adapter.mutate(lambda m: "noop") == "noop"
    assert _escalation_row_count(store) == 1


def test_mutate_escalation_standdowns_runs_rmw_inside_one_write_transaction() -> None:
    store = StateStore(":memory:")
    in_transaction_during_fn: list[bool] = []

    def _add(m: dict[int, StanddownRecord]) -> None:
        in_transaction_during_fn.append(store._conn.in_transaction)
        m[1] = _record("h1")

    store.mutate_escalation_standdowns(_add)
    assert in_transaction_during_fn == [True]
    assert store._conn.in_transaction is False
    assert set(store.load_escalation_standdowns()) == {1}


def test_mutate_escalation_standdowns_rolls_back_when_fn_raises() -> None:
    store = StateStore(":memory:")
    store.mutate_escalation_standdowns(lambda m: m.update({1: _record("keep")}))

    def _boom(m: dict[int, StanddownRecord]) -> None:
        m[2] = _record()
        raise RuntimeError("mutation failed")

    with pytest.raises(RuntimeError, match="mutation failed"):
        store.mutate_escalation_standdowns(_boom)
    assert store._conn.in_transaction is False
    assert set(store.load_escalation_standdowns()) == {1}


def test_escalation_adapters_satisfy_backend_protocol(mem_fs: dict[str, str]) -> None:
    assert isinstance(
        JsonEscalationLedger(_LEGACY_ESCALATION_DIR), EscalationLedgerBackend,
    )
    assert isinstance(
        SqliteEscalationLedger(StateStore(":memory:")), EscalationLedgerBackend,
    )


def test_json_and_sqlite_escalation_agree_via_legacy_seed(
    mem_fs: dict[str, str],
) -> None:
    """Seeding a mix of a canonical key, a legacy ``"<pr>@<head>"`` key, and a
    malformed line yields the same reads through both backends (the SQLite side
    seeded once from the JSON the JSON side reads directly)."""
    _seed_legacy_escalation({
        "10": {"head_sha": "h10", "delivered_at": "t", "serviced_comment_ids": [3]},
        "20@deadbeef": "2026-02-02T00:00:00+00:00",
        "bad": {"missing": "fields"},
    })
    json_be = JsonEscalationLedger(_LEGACY_ESCALATION_DIR)
    sqlite_be = SqliteEscalationLedger(
        StateStore(":memory:"), legacy_json_dir=_LEGACY_ESCALATION_DIR,
    )
    for pr in (10, 20, 999):
        assert has_standdown(json_be, pr) == has_standdown(sqlite_be, pr)
        assert unserviced_comment_ids(
            json_be, pr, {3, 4},
        ) == unserviced_comment_ids(sqlite_be, pr, {3, 4})
    # The malformed "bad" line is dropped on both sides; the legacy key migrated.
    assert set(json_be.read()) == set(sqlite_be.read()) == {10, 20}


def test_escalation_deliver_and_prune_parity_via_backends(
    mem_fs: dict[str, str],
) -> None:
    """A deliver → prune sequence over both backends stays in lock-step."""
    now = "2026-06-12T00:00:00+00:00"
    json_be = JsonEscalationLedger(Path("/fake/eb_j2"))
    sqlite_be = SqliteEscalationLedger(StateStore(":memory:"))
    for be, d in ((json_be, Path("/fake/eb_j2")), (sqlite_be, Path("/fake/eb_s2"))):
        assert deliver_handoff(
            d, _Handoff(pr_number=50, head_sha="h", stamp_count=2),
            backend=be, now=now,
            claim=StanddownClaim.HANDOFF_READY,
        ) is True
        assert prune_resolved_standdowns(be, lambda _pr: True) == [50]
        assert prune_expired_standdowns(be, now=now) == []
    assert json_be.read() == sqlite_be.read() == {}
