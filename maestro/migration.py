"""Schema validation and load-time normalisation for persistent memory files.

On startup, Maestro computes a fingerprint from the current dataclass field
definitions and compares it against the stored fingerprint in
``schema_version.json``.  If they differ, the stored fingerprint is simply
advanced: the dataclass loaders already drop unknown fields, coerce types, and
fill dataclass defaults **on read**, so a whole-file rewrite would produce
exactly the bytes the next read produces anyway.  Normalisation therefore
happens lazily, when each state file is next loaded — there is no eager rewrite
sweep.

Four genuine, data-*moving* steps run unconditionally on every call (they are
idempotent no-ops when there is nothing to do): the plan-content-hash backfill,
the default-limitation seeder, the one-shot move of leaked test-fixture
cost rows (``cost_tracking.LEAK_FENCE``) to ``cost_samples.cold.leak.jsonl``,
and the one-shot carry of the gap ledgers out of a retired ``charter.db``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import fields
from pathlib import Path

from maestro.consciousness import Consciousness, SelfModel
from maestro.cost_tracking import LEAK_COLD_FILENAME, LEAK_FENCE, _as_utc
from maestro.fileutil import atomic_write, file_lock
from maestro.gap_store import GAP_DB_FILENAME, carry_legacy_gap_rows
from maestro.journal import JournalEntry
from maestro.memory import (
    ArtifactReview,
    DocumentRecord,
    EvolutionPlan,
    EvolutionProposal,
    Memory,
    PlanStep,
    PublicationProposal,
    QuestionResolution,
    RejectionRecord,
    TaskRecord,
    VersionRecord,
)
from maestro.messaging import Message
from maestro.pr_ledger import PRLedgerEntry
from maestro.task_queue import QueuedTask

logger = logging.getLogger("maestro")

_SCHEMA_FILE = "schema_version.json"

# All dataclasses whose field names contribute to the schema fingerprint.
_TRACKED_CLASSES: tuple[type, ...] = (
    SelfModel,
    JournalEntry,
    TaskRecord,
    PlanStep,
    EvolutionPlan,
    Message,
    VersionRecord,
    RejectionRecord,
    ArtifactReview,
    PublicationProposal,
    DocumentRecord,
    QuestionResolution,
    QueuedTask,
    EvolutionProposal,
    PRLedgerEntry,
)

# State files that indicate existing persistent data.
_STATE_FILES: tuple[str, ...] = (
    "self_model.json",
    "journal.jsonl",
    "task_history.jsonl",
    "evolution_plans.jsonl",
    "messages.jsonl",
    "version_archive.jsonl",
    "rejection_history.jsonl",
    "artifact_reviews.jsonl",
    "publication_proposals.jsonl",
    "document_records.jsonl",
    "question_resolutions.jsonl",
    "self_model_archive.jsonl",
    "task_queue.json",
    "evolution_proposals.jsonl",
    "pr_ledger.json",
    GAP_DB_FILENAME,
)


def compute_schema_fingerprint() -> str:
    """Compute a deterministic SHA-256 fingerprint from tracked dataclass fields."""
    schema: dict[str, list[str]] = {}
    for cls in _TRACKED_CLASSES:
        schema[cls.__name__] = sorted(f.name for f in fields(cls))
    canonical = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_schema(memory_dir: Path) -> bool:
    """Return ``True`` if the stored schema fingerprint matches the current one.

    Also returns ``True`` when no state files exist yet (fresh install).
    """
    version_path = memory_dir / _SCHEMA_FILE
    if not version_path.exists():
        # No fingerprint stored — check whether any state files exist.
        return not _has_state_files(memory_dir)
    return _read_stored_fingerprint(version_path) == compute_schema_fingerprint()


def _has_state_files(memory_dir: Path) -> bool:
    """Return ``True`` if any persistent state files exist in *memory_dir*."""
    return any((memory_dir / name).exists() for name in _STATE_FILES)


def migrate_backfill_hashes(memory_dir: Path) -> bool:
    """Persist content hashes for plan rows that predate proposal 021.

    Reads the raw JSONL first: when every row already carries a
    non-empty ``plan_content_hash`` this is a no-op (no rewrite, no
    lock churn). Otherwise the file is rewritten once under
    ``file_lock`` through the normal loader, whose in-memory backfill
    (:meth:`Memory._parse_plan_line`) computes the missing hashes;
    non-empty stored hashes are never recomputed. Idempotent and
    re-runnable — invoked on every :func:`ensure_schema` call, like
    :func:`_seed_self_model`.

    Returns ``True`` on success or no-op, ``False`` when an exception
    was caught (consistent with the other startup steps).
    """
    path = memory_dir / "evolution_plans.jsonl"
    if not path.exists():
        return True
    try:
        mem = Memory(memory_dir)
        with file_lock(path):
            needs_backfill = False
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and not data.get("plan_content_hash"):
                    needs_backfill = True
                    break
            if not needs_backfill:
                return True
            plans: list[EvolutionPlan] = mem._load_all_plans()
            mem._save_all_plans(plans)
        logger.info(
            "Backfilled plan content hashes (%d plans)", len(plans),
        )
        return True
    except Exception:
        logger.warning("Failed to backfill plan content hashes", exc_info=True)
        return False


def _read_stored_fingerprint(version_path: Path) -> str | None:
    """Return the fingerprint stored at *version_path*, or ``None`` if invalid.

    Enforces the envelope contract :func:`_write_fingerprint` produces — a JSON
    object ``{"fingerprint": str}``. Unreadable or malformed JSON, a non-object
    envelope (``null``, a list, a scalar), and a missing or non-string
    ``fingerprint`` value all read as "no valid stored fingerprint", which the
    callers treat as a mismatch.
    """
    try:
        data = json.loads(version_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    stored = data.get("fingerprint")
    if not isinstance(stored, str):
        return None
    return stored


def _write_fingerprint(memory_dir: Path) -> None:
    """Persist the current schema fingerprint."""
    atomic_write(
        memory_dir / _SCHEMA_FILE,
        json.dumps({"fingerprint": compute_schema_fingerprint()}),
    )


def _seed_self_model(memory_dir: Path) -> bool:
    """Apply :data:`maestro.consciousness.DEFAULT_LIMITATIONS` to the self-model.

    Loads (or auto-creates) ``self_model.json`` via
    :class:`Consciousness`, applies any not-yet-seeded catalog entries
    via :meth:`Consciousness.seed_default_limitations`, and persists the
    model only when a mutation occurred. Idempotent: a key already in
    ``seeded_limitations`` is skipped, so an LLM REMOVE of the seeded
    entry is respected on subsequent runs.

    Returns ``True`` on success, ``False`` when an exception was caught
    (consistent with the other startup steps).
    """
    try:
        c = Consciousness(memory_dir)
        model = c.load_self_model()
        if Consciousness.seed_default_limitations(model):
            c.save_self_model(model)
            logger.info("Seeded default limitations into self_model.json")
        return True
    except Exception:
        logger.warning("Failed to seed default limitations", exc_info=True)
        return False


def _leak_prescan(path: Path) -> bool:
    """Return whether *path*'s frozen pre-cutoff prefix holds a fenced row.

    Rows are appended in time order, so the scan stops at the first row whose
    timestamp is past the fence cutoff; corrupt and non-dict lines are skipped.
    """
    cutoff = LEAK_FENCE.cutoff_epoch()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if LEAK_FENCE.matches(row):
                return True
            raw_ts = row.get("timestamp")
            if not isinstance(raw_ts, str):
                continue
            try:
                past = _as_utc(raw_ts).timestamp() > cutoff
            except (ValueError, OverflowError):
                continue
            if past:
                return False
    return False


def _move_leak_jsonl(memory_dir: Path) -> int:
    path = memory_dir / "cost_samples.jsonl"
    if not path.exists() or not _leak_prescan(path):
        return 0
    with file_lock(path):
        hot: list[str] = []
        leak: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                hot.append(line)
                continue
            if isinstance(row, dict) and LEAK_FENCE.matches(row):
                leak.append(line)
            else:
                hot.append(line)
        if not leak:
            return 0
        atomic_write(path, "".join(f"{ln}\n" for ln in hot))
        with open(memory_dir / LEAK_COLD_FILENAME, "a", encoding="utf-8") as fh:
            fh.write("".join(f"{ln}\n" for ln in leak))
    return len(leak)


def move_leak_cost_samples(
    memory_dir: Path, *, sqlite_state_enabled: bool = False,
) -> int:
    """Move fenced test-leak cost rows cold, once; return how many moved.

    The JSONL arm rewrites ``cost_samples.jsonl`` under the same
    :func:`file_lock` ``append_cost_sample`` and ``Memory.archive`` take,
    hot-first, then appends the leak lines to the cold file. With
    *sqlite_state_enabled* and a ``state.db`` present, the SQLite arm moves the
    same fenced rows in one transaction. Idempotent: a second run moves 0.
    """
    try:
        moved = _move_leak_jsonl(memory_dir)
        db_path = memory_dir / "state.db"
        if sqlite_state_enabled and db_path.exists():
            from maestro.state_store import StateStore

            moved += StateStore(db_path).move_cost_samples_to_cold(
                cutoff_epoch=LEAK_FENCE.cutoff_epoch(),
                callers=LEAK_FENCE.callers,
                cold_path=memory_dir / LEAK_COLD_FILENAME,
            )
    except (OSError, sqlite3.Error):
        logger.warning("Failed to move leaked cost samples cold", exc_info=True)
        return 0
    if moved:
        logger.info("Moved %d leaked test cost sample(s) to %s", moved, LEAK_COLD_FILENAME)
    return moved


def ensure_schema(memory_dir: Path, *, sqlite_state_enabled: bool = False) -> bool:
    """Advance the schema fingerprint and run the genuine startup steps.

    Call this **before** instantiating Memory, Consciousness, Journal, etc.
    so that components always load normalised data.

    On a fingerprint mismatch the stored fingerprint is advanced (via
    :func:`_write_fingerprint`) and nothing else is rewritten: the dataclass
    loaders drop unknown fields, coerce types, and fill dataclass defaults on
    read, so the whole-file rewrite an eager migration would perform produces
    exactly the bytes the next read produces anyway. Normalisation therefore
    happens lazily, the next time each state file is loaded. Because no rewrite
    runs, there is no partial-migration failure mode: the function always
    returns ``True``. The ``-> bool`` signature is retained for the callers and
    tests that assert ``ensure_schema(...) is True``.

    :func:`migrate_backfill_hashes`, :func:`_seed_self_model`,
    :func:`move_leak_cost_samples` and
    :func:`maestro.gap_store.carry_legacy_gap_rows` are invoked on **every**
    call, not only on fingerprint-mismatch paths. They are the four genuine
    data-*moving* steps (the gap carry runs only while a retired
    ``charter.db`` remains; the leak move rewrites only while a fenced row
    remains hot): the hash
    backfill only rewrites when a row lacks its ``plan_content_hash``; the seeder only re-saves when a catalog
    entry is not yet in ``seeded_limitations``. The steady-state cost is a
    handful of ``exists`` checks, one self-model read with no write, and the
    leak move's prescan, which JSON- and timestamp-parses the pre-cutoff
    prefix of ``cost_samples.jsonl`` on every call (that prefix stays hot
    until archived, so the scan is linear in it).
    """
    memory_dir.mkdir(parents=True, exist_ok=True)

    version_path = memory_dir / _SCHEMA_FILE
    current_fp = compute_schema_fingerprint()

    fingerprint_matches = (
        version_path.exists() and _read_stored_fingerprint(version_path) == current_fp
    )

    if fingerprint_matches:
        logger.debug("Schema fingerprint matches — no migration needed")
    elif not _has_state_files(memory_dir):
        # Fresh install: no state files to normalise. Write the initial
        # fingerprint so the mismatch is not re-detected on every startup. This
        # is a once-in-a-lifetime operator line — keep it distinct from the
        # genuine-mismatch message below so the log reflects what happened.
        logger.info(
            "No existing state files — writing initial schema fingerprint"
        )
        _write_fingerprint(memory_dir)
    else:
        # Load-time normalisation: the dataclass loaders normalise every row on
        # read, so no whole-file rewrite is needed on a fingerprint bump. Advance
        # the stored fingerprint so the mismatch is not re-detected on every
        # startup; the loaders fill the removed/added fields' defaults the next
        # time each state file is read.
        logger.info(
            "Schema mismatch detected — advancing fingerprint "
            "(loaders normalise on read; no rewrite)"
        )
        _write_fingerprint(memory_dir)

    migrate_backfill_hashes(memory_dir)
    _seed_self_model(memory_dir)
    move_leak_cost_samples(memory_dir, sqlite_state_enabled=sqlite_state_enabled)
    carried = carry_legacy_gap_rows(memory_dir)
    if carried:
        logger.info("Carried %d gap ledger row(s) from the retired charter.db", carried)
    return True
