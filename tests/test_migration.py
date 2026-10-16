"""Tests for schema validation and load-time normalisation."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import pytest

from maestro.consciousness import DEFAULT_LIMITATIONS, Consciousness, SelfModel
from maestro.journal import Journal, JournalEntry
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
from maestro.messaging import Message, MessageStore
from maestro.migration import (
    _SCHEMA_FILE,
    _STATE_FILES,
    _TRACKED_CLASSES,
    _has_state_files,
    _write_fingerprint,
    check_schema,
    compute_schema_fingerprint,
    ensure_schema,
    migrate_backfill_hashes,
)
from maestro.pr_ledger import PRLedgerEntry
from maestro.task_queue import QueuedTask, TaskQueue

DIR = Path("/fake/migration")


# --- compute_schema_fingerprint ---


def test_fingerprint_deterministic() -> None:
    assert compute_schema_fingerprint() == compute_schema_fingerprint()


def test_fingerprint_is_hex_string() -> None:
    fp = compute_schema_fingerprint()
    assert len(fp) == 64
    int(fp, 16)  # should not raise


def test_fingerprint_includes_all_tracked_classes() -> None:
    assert len(_TRACKED_CLASSES) == 15


# --- check_schema ---


def test_check_schema_no_files_returns_true(mem_fs: dict[str, str]) -> None:
    """Fresh install with no state files and no version file => True."""
    assert check_schema(DIR) is True


def test_check_schema_matching_fingerprint(mem_fs: dict[str, str]) -> None:
    fp = compute_schema_fingerprint()
    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"fingerprint": fp})
    assert check_schema(DIR) is True


def test_check_schema_mismatched_fingerprint(mem_fs: dict[str, str]) -> None:
    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"fingerprint": "stale"})
    assert check_schema(DIR) is False


def test_check_schema_corrupted_version_file(mem_fs: dict[str, str]) -> None:
    mem_fs[str(DIR / _SCHEMA_FILE)] = "not json"
    assert check_schema(DIR) is False


def test_check_schema_no_version_but_state_files_exist(mem_fs: dict[str, str]) -> None:
    """State files exist but no version file => mismatch."""
    mem_fs[str(DIR / "self_model.json")] = "{}"
    assert check_schema(DIR) is False


def test_check_schema_missing_fingerprint_key(mem_fs: dict[str, str]) -> None:
    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"other": "value"})
    assert check_schema(DIR) is False


_NON_OBJECT_ENVELOPES = ["null", "[]", "[1, 2]", "42", '"fp"', "true"]


@pytest.mark.parametrize("raw", _NON_OBJECT_ENVELOPES)
def test_check_schema_non_object_envelope_is_mismatch(
    mem_fs: dict[str, str], raw: str
) -> None:
    mem_fs[str(DIR / _SCHEMA_FILE)] = raw
    assert check_schema(DIR) is False


@pytest.mark.parametrize("value", [123, None])
def test_check_schema_non_string_fingerprint_is_mismatch(
    mem_fs: dict[str, str], value: object
) -> None:
    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"fingerprint": value})
    assert check_schema(DIR) is False


# --- ensure_schema ---


def test_ensure_schema_fresh_install(mem_fs: dict[str, str]) -> None:
    """First run with no state files writes fingerprint."""
    # Fresh install is a success exit — the return contract reports True (#224).
    assert ensure_schema(DIR) is True

    version_path = str(DIR / _SCHEMA_FILE)
    assert version_path in mem_fs
    data = json.loads(mem_fs[version_path])
    assert data["fingerprint"] == compute_schema_fingerprint()


def test_ensure_schema_fresh_install_logs_initial_fingerprint(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A fresh install logs the initial-fingerprint line, not a mismatch.

    Regression: the collapsed schema-on-read else-branch (proposal 106) logged
    the genuine-mismatch line even on a first-ever boot with no state files,
    where the accurate operator line is "writing initial schema fingerprint".
    """
    with caplog.at_level(logging.INFO, logger="maestro"):
        ensure_schema(DIR)

    assert "writing initial schema fingerprint" in caplog.text
    assert "Schema mismatch detected" not in caplog.text


def test_ensure_schema_mismatch_logs_advance_not_fresh(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuine mismatch (state files present) logs the advance line, not fresh.

    The companion to the fresh-install case: when state files exist the log must
    report a mismatch/advance, never the "No existing state files" line.
    """
    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"fingerprint": "old_hash"})
    mem_fs[str(DIR / "self_model.json")] = json.dumps(
        asdict(SelfModel(awakened_at="2025-01-01"))
    )

    with caplog.at_level(logging.INFO, logger="maestro"):
        ensure_schema(DIR)

    assert "Schema mismatch detected" in caplog.text
    assert "No existing state files" not in caplog.text


def test_ensure_schema_matching_noop(mem_fs: dict[str, str]) -> None:
    """When fingerprint matches and seeds are present, no file write occurs."""
    fp = compute_schema_fingerprint()
    version_path = str(DIR / _SCHEMA_FILE)
    mem_fs[version_path] = json.dumps({"fingerprint": fp})

    # Pre-seed the catalog so ``_seed_self_model`` is a no-op on the
    # steady-state path; otherwise it would mutate ``self_model.json`` to
    # apply the missing seeds.
    seeded = [k for k, _ in DEFAULT_LIMITATIONS]
    model = asdict(SelfModel(
        awakened_at="2025-01-01",
        seeded_limitations=seeded,
    ))
    model["extra"] = "should remain"
    mem_fs[str(DIR / "self_model.json")] = json.dumps(model)

    # Fingerprint-match no-op is a success exit — the return contract reports
    # True (#224).
    assert ensure_schema(DIR) is True

    # Extra field should still be there since no migration ran and the
    # seeder found nothing to apply.
    result = json.loads(mem_fs[str(DIR / "self_model.json")])
    assert "extra" in result


def test_ensure_schema_mismatch_advances_fingerprint(mem_fs: dict[str, str]) -> None:
    """On a fingerprint mismatch, ensure_schema advances the stored fingerprint.

    No whole-file rewrite runs (the loaders normalise on read); the self-model's
    un-normalised ``extra`` field is dropped only because the seeder re-saves the
    normalised model — the seed catalog is not yet applied, so the seeder mutates
    and persists.
    """
    version_path = str(DIR / _SCHEMA_FILE)
    mem_fs[version_path] = json.dumps({"fingerprint": "old_hash"})

    model = asdict(SelfModel(awakened_at="2025-01-01"))
    model["extra"] = "should be removed"
    mem_fs[str(DIR / "self_model.json")] = json.dumps(model)

    ensure_schema(DIR)

    # Extra field is gone — the seeder re-saved the normalised model.
    result = json.loads(mem_fs[str(DIR / "self_model.json")])
    assert "extra" not in result

    # Fingerprint is advanced to the current schema.
    data = json.loads(mem_fs[version_path])
    assert data["fingerprint"] == compute_schema_fingerprint()


def test_ensure_schema_no_version_file_with_state(mem_fs: dict[str, str]) -> None:
    """State files exist but no version file => advance fingerprint (no rewrite).

    The self-model's ``extra`` field is dropped by the seeder's normalised
    re-save, and the fingerprint is written.
    """
    model = asdict(SelfModel(awakened_at="2025-01-01"))
    model["extra"] = "removed"
    mem_fs[str(DIR / "self_model.json")] = json.dumps(model)

    ensure_schema(DIR)

    result = json.loads(mem_fs[str(DIR / "self_model.json")])
    assert "extra" not in result

    data = json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])
    assert data["fingerprint"] == compute_schema_fingerprint()


def test_ensure_schema_idempotent(mem_fs: dict[str, str]) -> None:
    """Running ensure_schema twice should be safe."""
    ensure_schema(DIR)
    fp1 = json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])["fingerprint"]
    ensure_schema(DIR)
    fp2 = json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])["fingerprint"]
    assert fp1 == fp2


# --- schema-on-read normalisation (proposal 106) ---


def _seed_old_schema_corpus(mem_fs: dict[str, str], root: Path) -> None:
    """Seed a corpus of old-schema records under *root*.

    Rows carry extra (dropped-on-read) fields and the plan row omits
    newly-added fields — the fixture behind the "every loader normalises
    on read" contract that makes the deleted rewrite path redundant.
    """
    self_model = asdict(SelfModel(
        awakened_at="2025-01-01T00:00:00",
        # Pre-seed the catalog so ``_seed_self_model`` is a no-op (it never
        # re-saves), so the on-disk self_model.json keeps its un-normalised
        # bytes and the loader — not the seeder — is what drops the extra field.
        seeded_limitations=[k for k, _ in DEFAULT_LIMITATIONS],
    ))
    self_model["extra_field"] = "drop me"
    mem_fs[str(root / "self_model.json")] = json.dumps(self_model)

    journal = asdict(JournalEntry(
        entry_id="e1", timestamp="t1",
        entry_type="reflection", content="hello",
        triggered_by="contemplation",
    ))
    journal["extra"] = "drop me"
    mem_fs[str(root / "journal.jsonl")] = json.dumps(journal) + "\n"

    task = asdict(TaskRecord(
        task_id="t1", goal="g",
        plan=[PlanStep(step_id="s1", description="do", status="done")],
        outcome="success", result_summary="ok", errors=[],
        duration_secs=1.0, timestamp="ts1",
    ))
    task["extra"] = "drop me"
    mem_fs[str(root / "task_history.jsonl")] = json.dumps(task) + "\n"

    # Legacy plan row missing the PR-tending / hash fields.
    plan = {
        "plan_id": "p1", "title": "t", "stage": "awakening",
        "category": "capability", "rationale": "r",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "priority": "capability",
        "status": "implemented", "created_at": "2024-01-01T00:00:00Z",
    }
    mem_fs[str(root / "evolution_plans.jsonl")] = json.dumps(plan) + "\n"

    version = asdict(VersionRecord(
        version_id="v1", plan_id="p1", plan_title="t", commit_sha="sha",
        merged_at="2026-01-01T00:00:00+00:00", window_tasks_total=1,
        window_tasks_succeeded=1, window_tasks_failed=0,
        window_sdk_cost_usd=0.0, window_sdk_calls=1,
        window_cache_hit_rate=0.0, window_rate_limit_hits=0,
        window_error_count=0, quality_gate_pass=True, notes=None,
    ))
    version["future_field"] = "drop me"
    mem_fs[str(root / "version_archive.jsonl")] = json.dumps(version) + "\n"

    rejection = asdict(RejectionRecord(
        plan_id="p1", title="t", category="capability",
        priority="capability", rejection_reason="no",
        timestamp="2026-04-23T00:00:00+00:00",
        rejection_category="scope_too_large",
    ))
    rejection["future_field"] = "drop me"
    mem_fs[str(root / "rejection_history.jsonl")] = json.dumps(rejection) + "\n"

    review = {
        "review_id": "r1", "task_id": "t1", "verdict": "publish",
        "title": "x", "reasoning": "y", "bundle_file_names": ["a"],
        "proposed_name": "n", "proposed_description": "d",
        "timestamp": "2026-04-23T00:00:00+00:00",
        "legacy_removed_field": "drop me",
    }
    mem_fs[str(root / "artifact_reviews.jsonl")] = json.dumps(review) + "\n"

    document = {
        "doc_id": "d1", "doc_type": "core", "title": "Sample",
        "linked_task_ids": ["t1"], "target_path": None, "doc_branch": None,
        "pr_url": None, "bundle_file_names": [], "reasoning": "r",
        "status": "pending", "error": None,
        "created_at": "2026-04-25T00:00:00+00:00",
        "legacy_removed_field": "drop me",
    }
    mem_fs[str(root / "document_records.jsonl")] = json.dumps(document) + "\n"

    resolution = {
        "resolution_id": "qr1", "iteration": 7,
        "timestamp": "2026-04-26T00:00:00+00:00", "question": "What am I?",
        "task_id": "t1", "outcome": "success", "disposition": "removed",
        "finding": "answer", "legacy_field": "drop me",
    }
    mem_fs[str(root / "question_resolutions.jsonl")] = (
        json.dumps(resolution) + "\n"
    )

    message = asdict(Message(
        message_id="m1", timestamp="t1", sender="maestro",
        content="hi", message_type="info",
    ))
    message["extra"] = "drop me"
    mem_fs[str(root / "messages.jsonl")] = json.dumps(message) + "\n"

    queued = asdict(QueuedTask(
        task_id="q1", goal="do a thing",
        created_at="2026-04-27T00:00:00+00:00",
        last_touched_at="2026-04-27T00:00:00+00:00",
    ))
    queued["extra"] = "drop me"
    mem_fs[str(root / "task_queue.json")] = json.dumps([queued])


def test_all_loaders_normalise_legacy_corpus_on_read(mem_fs: dict[str, str]) -> None:
    """Every canonical loader normalises a legacy corpus on read (proposal 106).

    ``ensure_schema`` advances the fingerprint but performs no whole-file
    rewrite, so each state file keeps its un-normalised on-disk bytes. Seed a
    full old-schema corpus (extra fields, a plan row missing newly-added
    fields), run ``ensure_schema``, and assert
    every loader returns a fully-normalised record — the safety net that makes
    the deleted rewrite path redundant.
    """
    root = Path("/fake/normalise/root")
    # A stale fingerprint drives the mismatch branch.
    mem_fs[str(root / _SCHEMA_FILE)] = json.dumps({"fingerprint": "stale"})
    _seed_old_schema_corpus(mem_fs, root)

    assert ensure_schema(root) is True

    mem = Memory(root)
    # Each loader returns exactly the seeded record, normalised on read.
    [entry] = Journal(root)._load_all()
    assert entry.entry_id == "e1"
    assert not hasattr(entry, "extra")

    [task] = mem._load_all_tasks()
    assert task.task_id == "t1"
    assert not hasattr(task, "extra")

    [plan] = mem._load_all_plans()
    assert plan.plan_id == "p1"
    # The legacy plan row omitted the PR-tending fields; the loader fills them.
    assert plan.last_pr_reminder_at is None

    [version] = mem._load_all_versions()
    assert version.version_id == "v1"
    assert not hasattr(version, "future_field")

    [rejection] = mem._load_all_rejections()
    assert rejection.plan_id == "p1"

    [review] = mem._load_all_reviews()
    assert review.review_id == "r1"

    [document] = mem._load_all_documents()
    assert document.doc_id == "d1"

    [resolution] = mem._load_all_question_resolutions()
    assert resolution.resolution_id == "qr1"

    [message] = MessageStore(root)._load_all()
    assert message.message_id == "m1"

    [item] = TaskQueue(root / "task_queue.json").load()
    assert item.task_id == "q1"

    # The self-model is pre-seeded, so the seeder never re-saves: the on-disk
    # bytes keep ``extra_field``, proving the LOADER (not the seeder) drops it.
    raw_self_model = json.loads(mem_fs[str(root / "self_model.json")])
    assert "extra_field" in raw_self_model
    loaded_self_model = Consciousness(root).load_self_model()
    assert not hasattr(loaded_self_model, "extra_field")


def test_ensure_schema_advances_fingerprint_and_normalises_on_read(
    mem_fs: dict[str, str],
) -> None:
    """No whole-file rewrite runs, yet the genuine data-moving steps still fire.

    Proves the two things ``ensure_schema`` must do on a mismatch (proposal
    106): the fingerprint is advanced and the plan-hash backfill fires — while the un-normalised state-file bytes are
    left untouched (no rewrite) because reads normalise them anyway.
    """
    from unittest.mock import patch as _patch

    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"fingerprint": "stale"})

    journal = asdict(JournalEntry(
        entry_id="e1", timestamp="t1",
        entry_type="reflection", content="hello",
        triggered_by="contemplation",
    ))
    journal["extra"] = "drop me"
    mem_fs[str(DIR / "journal.jsonl")] = json.dumps(journal) + "\n"

    # Legacy plan row with no plan_content_hash -> backfill must fill it.
    mem_fs[str(DIR / "evolution_plans.jsonl")] = (
        json.dumps(_hashless_plan_row("p1")) + "\n"
    )

    original = __import__(
        "maestro.fileutil", fromlist=["atomic_write"],
    ).atomic_write
    written: list[str] = []

    def recording_atomic_write(path: Path, content: str) -> None:
        written.append(str(path))
        original(path, content)

    with _patch(
        "maestro.migration.atomic_write", side_effect=recording_atomic_write,
    ):
        assert ensure_schema(DIR) is True

    # No normalization rewrite: journal.jsonl was NOT written through
    # migration.atomic_write, and its un-normalised bytes survive.
    assert not any("journal.jsonl" in p for p in written)
    raw_journal = json.loads(mem_fs[str(DIR / "journal.jsonl")].strip())
    assert "extra" in raw_journal

    # ...yet the loader still normalises on read (the extra field is dropped).
    [loaded] = Journal(DIR)._load_all()
    assert loaded.entry_id == "e1"
    assert not hasattr(loaded, "extra")

    # (a) The fingerprint was advanced so the mismatch is not re-detected.
    assert any(_SCHEMA_FILE in p for p in written)
    assert check_schema(DIR) is True
    # (b) The plan-hash backfill still fired.
    raw_plan = json.loads(mem_fs[str(DIR / "evolution_plans.jsonl")].strip())
    assert raw_plan["plan_content_hash"]


# --- _write_fingerprint ---


def test_write_fingerprint(mem_fs: dict[str, str]) -> None:
    _write_fingerprint(DIR)
    data = json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])
    assert data["fingerprint"] == compute_schema_fingerprint()


def test_ensure_schema_corrupted_version_advances_fingerprint(mem_fs: dict[str, str]) -> None:
    """A corrupted version file is treated as a mismatch: the fingerprint is
    advanced (the seeder's normalised re-save drops the self-model extra field)."""
    mem_fs[str(DIR / _SCHEMA_FILE)] = "not json"
    model = asdict(SelfModel(awakened_at="2025-01-01"))
    model["extra"] = "removed"
    mem_fs[str(DIR / "self_model.json")] = json.dumps(model)

    ensure_schema(DIR)

    result = json.loads(mem_fs[str(DIR / "self_model.json")])
    assert "extra" not in result
    data = json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])
    assert data["fingerprint"] == compute_schema_fingerprint()


@pytest.mark.parametrize("raw", _NON_OBJECT_ENVELOPES)
def test_ensure_schema_non_object_envelope_advances_fingerprint(
    mem_fs: dict[str, str], raw: str
) -> None:
    mem_fs[str(DIR / _SCHEMA_FILE)] = raw
    mem_fs[str(DIR / "self_model.json")] = json.dumps(
        asdict(SelfModel(awakened_at="2025-01-01"))
    )

    assert ensure_schema(DIR) is True

    data = json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])
    assert data["fingerprint"] == compute_schema_fingerprint()


@pytest.mark.parametrize("raw", _NON_OBJECT_ENVELOPES)
def test_ensure_schema_non_object_envelope_fresh_install_rewrites(
    mem_fs: dict[str, str], raw: str
) -> None:
    mem_fs[str(DIR / _SCHEMA_FILE)] = raw

    assert ensure_schema(DIR) is True

    data = json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])
    assert data["fingerprint"] == compute_schema_fingerprint()


def test_state_files_covers_known_files() -> None:
    expected = {
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
        "gaps.db",
    }
    assert set(_STATE_FILES) == expected


def test_fingerprint_includes_artifact_review_and_publication_proposal() -> None:
    """The new dataclasses must be part of the fingerprinted schema.

    Guards against accidental removal from ``_TRACKED_CLASSES`` — if either
    is dropped, the fingerprint no longer reflects their schema and a
    later field change could silently bypass migration.
    """
    assert ArtifactReview in _TRACKED_CLASSES
    assert PublicationProposal in _TRACKED_CLASSES
    assert EvolutionProposal in _TRACKED_CLASSES


def test_fingerprint_includes_pr_ledger_entry() -> None:
    """The PR-ledger dataclass and its state file must be registered.

    Guards against ``PRLedgerEntry`` being dropped from ``_TRACKED_CLASSES``
    (a field change would then silently bypass the schema fingerprint) or
    ``pr_ledger.json`` being dropped from ``_STATE_FILES`` (a pr-ledger-only
    data dir would then be misclassified as a fresh install).
    """
    assert PRLedgerEntry in _TRACKED_CLASSES
    assert "pr_ledger.json" in _STATE_FILES


def test_gap_db_only_dir_is_not_fresh_install(mem_fs: dict[str, str]) -> None:
    """A data dir holding only the gap store is existing state."""
    assert _has_state_files(DIR) is False
    mem_fs[str(DIR / "gaps.db")] = ""
    assert _has_state_files(DIR) is True


def test_every_state_file_dataclass_is_tracked() -> None:
    """Completeness guard over the files already registered in
    ``_STATE_FILES``: each one's backing dataclass must be part of the schema
    fingerprint.

    Registration into ``_TRACKED_CLASSES`` / ``_STATE_FILES`` is manual
    convention. This maps each ``_STATE_FILES`` entry to the dataclass whose
    rows it persists and asserts that dataclass is fingerprinted (or is
    explicitly declared dataclass-less); the final coverage assertion forces
    every ``_STATE_FILES`` entry to be classified here — mapped to a tracked
    dataclass or declared dataclass-less — rather than slipping in unnoticed.

    Scope: this catches a file *already in* ``_STATE_FILES`` whose backing
    dataclass was never added to ``_TRACKED_CLASSES``. It does **not** catch a
    durable store that was never added to ``_STATE_FILES`` at all — which was
    the actual shape of #2982 (``pr_ledger.json`` absent from the set). That
    gap is closed upstream by ``test_state_files_covers_known_files`` and
    ``test_fingerprint_includes_pr_ledger_entry`` pinning the expected set,
    not by this guard.
    """
    state_file_dataclass: dict[str, type] = {
        "self_model.json": SelfModel,
        "journal.jsonl": JournalEntry,
        "task_history.jsonl": TaskRecord,
        "evolution_plans.jsonl": EvolutionPlan,
        "messages.jsonl": Message,
        "version_archive.jsonl": VersionRecord,
        "rejection_history.jsonl": RejectionRecord,
        "artifact_reviews.jsonl": ArtifactReview,
        "publication_proposals.jsonl": PublicationProposal,
        "document_records.jsonl": DocumentRecord,
        "question_resolutions.jsonl": QuestionResolution,
        "task_queue.json": QueuedTask,
        "evolution_proposals.jsonl": EvolutionProposal,
        "pr_ledger.json": PRLedgerEntry,
    }
    # State files whose rows are ad-hoc dicts with no single backing
    # dataclass, so they contribute nothing to the fingerprint by design.
    dataclass_less = {
        "self_model_archive.jsonl",
        # SQLite files versioned by their own PRAGMA user_version.
        "gaps.db",
    }

    for state_file, cls in state_file_dataclass.items():
        assert cls in _TRACKED_CLASSES, (
            f"{state_file} is backed by untracked {cls.__name__}"
        )

    assert set(state_file_dataclass) | dataclass_less == set(_STATE_FILES)


def test_ensure_schema_ignores_absent_artifact_files(mem_fs: dict[str, str]) -> None:
    """ensure_schema() runs cleanly when the new JSONL files are absent."""
    ensure_schema(DIR)

    version_path = str(DIR / _SCHEMA_FILE)
    assert version_path in mem_fs
    assert str(DIR / "artifact_reviews.jsonl") not in mem_fs
    assert str(DIR / "publication_proposals.jsonl") not in mem_fs


def test_ensure_schema_tolerates_existing_artifact_files(mem_fs: dict[str, str]) -> None:
    """When the new JSONL files already exist with valid rows, ensure_schema
    completes without error.  ``ensure_schema`` never rewrites state files, so
    they are left untouched (the loaders normalise them on read)."""
    review_path = str(DIR / "artifact_reviews.jsonl")
    proposal_path = str(DIR / "publication_proposals.jsonl")
    review = {
        "review_id": "r1",
        "task_id": "t1",
        "verdict": "publish",
        "title": "nice",
        "reasoning": "good",
        "bundle_file_names": ["README.md"],
        "proposed_name": "nice-thing",
        "proposed_description": "a thing",
        "timestamp": "2026-04-23T00:00:00Z",
    }
    proposal = {
        "proposal_id": "p1",
        "task_id": "t1",
        "review_id": "r1",
        "proposed_name": "nice-thing",
        "description": "a thing",
        "proposed_readme": None,
        "publishable_files": ["README.md"],
        "status": "pending_approval",
        "created_at": "2026-04-23T00:00:00Z",
    }
    mem_fs[review_path] = json.dumps(review) + "\n"
    mem_fs[proposal_path] = json.dumps(proposal) + "\n"

    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"fingerprint": "stale"})

    ensure_schema(DIR)

    # Files remain present; no migrator was registered for them.
    assert review_path in mem_fs
    assert proposal_path in mem_fs

    # Rows still load after the funding fields were dropped.
    memory = Memory(DIR)
    assert len(memory.get_artifact_reviews()) == 1
    assert len(memory.get_publication_proposals()) == 1


# --- _seed_self_model wired into ensure_schema ---


def test_ensure_schema_seeds_self_model_on_fresh_install(
    mem_fs: dict[str, str],
) -> None:
    ensure_schema(DIR)
    self_model_path = str(DIR / "self_model.json")
    assert self_model_path in mem_fs
    data = json.loads(mem_fs[self_model_path])
    seed_key, seed_text = DEFAULT_LIMITATIONS[0]
    assert seed_key in data["seeded_limitations"]
    assert seed_text in data["limitations_known"]


def test_ensure_schema_seed_is_idempotent_across_runs(
    mem_fs: dict[str, str],
) -> None:
    ensure_schema(DIR)
    first_payload = mem_fs[str(DIR / "self_model.json")]
    ensure_schema(DIR)
    second_payload = mem_fs[str(DIR / "self_model.json")]
    assert first_payload == second_payload


def test_ensure_schema_does_not_re_seed_after_llm_removal(
    mem_fs: dict[str, str],
) -> None:
    ensure_schema(DIR)
    seed_key, seed_text = DEFAULT_LIMITATIONS[0]

    c = Consciousness(DIR)
    model = c.load_self_model()
    model.limitations_known.remove(seed_text)
    c.save_self_model(model)
    assert seed_key in c.load_self_model().seeded_limitations

    # Force a re-run by clearing the fingerprint so ensure_schema enters
    # the seed branch a second time. The seed key already in
    # seeded_limitations must keep the seed text from being re-added.
    mem_fs.pop(str(DIR / _SCHEMA_FILE), None)
    ensure_schema(DIR)
    after = c.load_self_model()
    assert seed_text not in after.limitations_known
    assert seed_key in after.seeded_limitations


def test_ensure_schema_seeds_after_fingerprint_advance(
    mem_fs: dict[str, str],
) -> None:
    """A pre-existing self_model.json without the seed gets seeded on a mismatch."""
    legacy_model = asdict(SelfModel(awakened_at="2025-01-01"))
    legacy_model.pop("seeded_limitations", None)
    mem_fs[str(DIR / "self_model.json")] = json.dumps(legacy_model)
    mem_fs[str(DIR / _SCHEMA_FILE)] = json.dumps({"fingerprint": "old_hash"})

    ensure_schema(DIR)

    seed_key, seed_text = DEFAULT_LIMITATIONS[0]
    data = json.loads(mem_fs[str(DIR / "self_model.json")])
    assert seed_key in data["seeded_limitations"]
    assert seed_text in data["limitations_known"]
    assert (
        json.loads(mem_fs[str(DIR / _SCHEMA_FILE)])["fingerprint"]
        == compute_schema_fingerprint()
    )


def test_seed_self_model_logs_and_returns_false_on_exception(
    mem_fs: dict[str, str],
) -> None:
    """When loading the self-model raises, the seeder logs and returns False."""
    from unittest.mock import patch as _patch

    from maestro.migration import _seed_self_model

    with _patch.object(
        Consciousness, "load_self_model",
        side_effect=RuntimeError("boom"),
    ):
        assert _seed_self_model(DIR) is False


def test_ensure_schema_seeds_new_catalog_entry_on_matching_fingerprint(
    mem_fs: dict[str, str],
) -> None:
    """Adding a catalog entry without a schema change still seeds existing instances.

    Future ``DEFAULT_LIMITATIONS`` appends don't move the schema
    fingerprint (the catalog is data, not a struct field), so the seeder
    must run on the steady-state ``ensure_schema`` path — not only after
    a fingerprint mismatch — for new entries to land on already-migrated
    instances.
    """
    from unittest.mock import patch as _patch

    ensure_schema(DIR)
    seed_key, _seed_text = DEFAULT_LIMITATIONS[0]
    assert seed_key in json.loads(
        mem_fs[str(DIR / "self_model.json")],
    )["seeded_limitations"]

    new_key = "future_seed_v1"
    new_text = "Hypothetical future limitation for catalog-extension test."
    extended = (*DEFAULT_LIMITATIONS, (new_key, new_text))
    with _patch("maestro.consciousness.DEFAULT_LIMITATIONS", extended):
        ensure_schema(DIR)

    data = json.loads(mem_fs[str(DIR / "self_model.json")])
    assert new_key in data["seeded_limitations"]
    assert new_text in data["limitations_known"]


# --- migrate_backfill_hashes (proposal 021) ---


def _hashless_plan_row(plan_id: str = "bf1") -> dict[str, object]:
    """A raw plan row as written before plan_content_hash existed."""
    row = asdict(EvolutionPlan(
        plan_id=plan_id, title="legacy plan",
        category="capability", rationale="old", proposed_changes=["a"],
        files_affected=["b.py"], expected_impact="none",
        created_at="2024-01-01T00:00:00Z",
    ))
    del row["plan_content_hash"]
    del row["notified_content_hash"]
    return row


def test_backfill_hashes_populates_empty(mem_fs: dict[str, str]) -> None:
    """Legacy rows gain a persisted, content-matching hash."""
    from maestro.memory import Memory, compute_plan_content_hash

    path = str(DIR / "evolution_plans.jsonl")
    mem_fs[path] = json.dumps(_hashless_plan_row()) + "\n"

    assert migrate_backfill_hashes(DIR) is True

    row = json.loads(mem_fs[path].strip())
    assert row["plan_content_hash"] != ""
    loaded = Memory(DIR).get_plan("bf1")
    assert loaded is not None
    assert row["plan_content_hash"] == compute_plan_content_hash(loaded)


def test_backfill_hashes_noop_when_all_hashed(mem_fs: dict[str, str]) -> None:
    """A second invocation does not rewrite the file (idempotent no-op)."""
    from unittest.mock import patch

    from maestro.memory import Memory

    path = str(DIR / "evolution_plans.jsonl")
    mem_fs[path] = json.dumps(_hashless_plan_row()) + "\n"
    assert migrate_backfill_hashes(DIR) is True
    after_first = mem_fs[path]

    with patch.object(Memory, "_save_all_plans", autospec=True) as save_spy:
        assert migrate_backfill_hashes(DIR) is True
    save_spy.assert_not_called()
    assert mem_fs[path] == after_first


def test_backfill_hashes_preserves_nonempty_hash(mem_fs: dict[str, str]) -> None:
    """A non-empty stored hash is never recomputed, even when wrong."""
    path = str(DIR / "evolution_plans.jsonl")
    stale = _hashless_plan_row("bf_keep")
    stale["plan_content_hash"] = "deadbeef" * 8
    rows = [json.dumps(stale), json.dumps(_hashless_plan_row("bf_fill"))]
    mem_fs[path] = "\n".join(rows) + "\n"

    assert migrate_backfill_hashes(DIR) is True

    by_id = {
        json.loads(line)["plan_id"]: json.loads(line)
        for line in mem_fs[path].strip().splitlines()
    }
    assert by_id["bf_keep"]["plan_content_hash"] == "deadbeef" * 8
    assert by_id["bf_fill"]["plan_content_hash"] != ""


def test_backfill_hashes_missing_file_noop(mem_fs: dict[str, str]) -> None:
    assert migrate_backfill_hashes(DIR) is True
    assert str(DIR / "evolution_plans.jsonl") not in mem_fs


def test_backfill_hashes_skips_unparseable_lines(mem_fs: dict[str, str]) -> None:
    """Corrupted, blank, and non-dict lines are skipped while scanning for
    rows that need a backfill."""
    path = str(DIR / "evolution_plans.jsonl")
    mem_fs[path] = (
        "not json\n"
        "\n"
        + json.dumps(["a", "list"]) + "\n"
        + json.dumps(_hashless_plan_row("bf2")) + "\n"
    )

    assert migrate_backfill_hashes(DIR) is True

    rows = [json.loads(line) for line in mem_fs[path].strip().splitlines()]
    by_id = {r["plan_id"]: r for r in rows if isinstance(r, dict) and "plan_id" in r}
    assert by_id["bf2"]["plan_content_hash"] != ""


def test_backfill_hashes_error_returns_false(mem_fs: dict[str, str]) -> None:
    """An exception during the rewrite is logged and reported as False."""
    from unittest.mock import patch

    from maestro.memory import Memory

    path = str(DIR / "evolution_plans.jsonl")
    mem_fs[path] = json.dumps(_hashless_plan_row("bf3")) + "\n"

    with patch.object(
        Memory, "_load_all_plans", autospec=True,
        side_effect=RuntimeError("boom"),
    ):
        assert migrate_backfill_hashes(DIR) is False


def test_ensure_schema_backfills_hashes(mem_fs: dict[str, str]) -> None:
    """ensure_schema persists hashes even when the fingerprint matches."""
    path = str(DIR / "evolution_plans.jsonl")
    mem_fs[path] = json.dumps(_hashless_plan_row("bf4")) + "\n"
    _write_fingerprint(DIR)

    ensure_schema(DIR)

    row = json.loads(mem_fs[path].strip())
    assert row["plan_content_hash"] != ""


def test_ensure_schema_logs_carried_gap_rows(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import patch

    with (
        patch("maestro.migration.carry_legacy_gap_rows", return_value=3) as carry,
        caplog.at_level(logging.INFO, logger="maestro"),
    ):
        assert ensure_schema(Path("/fake/mem")) is True
    carry.assert_called_once_with(Path("/fake/mem"))
    assert "Carried 3 gap ledger row(s)" in caplog.text
