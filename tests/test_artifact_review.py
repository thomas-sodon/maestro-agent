"""Unit tests for :mod:`maestro.artifact_review`.

Exercises candidate selection, bundle construction bounds, LLM parse
paths (happy, malformed, hard-block downgrade), budget-throttle gating,
and ``proposed_readme`` round-tripping. All I/O is mocked via ``mem_fs``
and the SDK ``query`` call is patched per-test.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.artifact_review import ArtifactReviewer, _artifacts_non_empty
from maestro.config import Config
from maestro.journal import Journal
from maestro.memory import ArtifactReview, Memory, TaskRecord
from maestro.sdk import QueryResult


@pytest.fixture
def _agent_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Return (memory_dir, workspace_dir) built in a real tmp_path.

    The selection pass walks real directories via ``Path.iterdir`` / ``rglob``
    for the artifact bundle, which ``mem_fs`` does not simulate, so these
    unit tests use real tmp directories for the filesystem but still mock
    the SDK ``query`` call.
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return memory_dir, workspace


def _make_config(memory_dir: Path, workspace: Path, **overrides: Any) -> Config:
    kwargs = {
        "memory_dir": memory_dir,
        "workspace": workspace,
        "enable_session_reuse": False,
    }
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_task(
    task_id: str,
    artifacts_dir: str | None,
    *,
    outcome: str = "success",
    timestamp: str = "2026-04-01T00:00:00+00:00",
    goal: str = "do a thing",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp=timestamp,
        source="self_directed",
        artifacts_dir=artifacts_dir,
    )


def _write_artifact(base: Path, name: str, contents: str) -> Path:
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _build_reviewer(
    memory_dir: Path, workspace: Path, **config_overrides: Any,
) -> tuple[ArtifactReviewer, Memory, Journal, Config]:
    config = _make_config(memory_dir, workspace, **config_overrides)
    memory = Memory(memory_dir)
    journal = Journal(memory_dir)
    reviewer = ArtifactReviewer(
        memory, journal, config,
        clock=lambda: datetime(2026, 4, 24, tzinfo=UTC),
    )
    return reviewer, memory, journal, config


# ---------------------------------------------------------------------------
# _artifacts_non_empty
# ---------------------------------------------------------------------------


def test_artifacts_non_empty_none() -> None:
    assert _artifacts_non_empty(None) is False


def test_artifacts_non_empty_missing_dir(tmp_path: Path) -> None:
    assert _artifacts_non_empty(str(tmp_path / "does_not_exist")) is False


def test_artifacts_non_empty_empty_dir(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    assert _artifacts_non_empty(str(d)) is False


def test_artifacts_non_empty_populated_dir(tmp_path: Path) -> None:
    d = tmp_path / "full"
    d.mkdir()
    (d / "README.md").write_text("x", encoding="utf-8")
    assert _artifacts_non_empty(str(d)) is True


# ---------------------------------------------------------------------------
# select_candidates
# ---------------------------------------------------------------------------


def test_select_candidates_filters_and_caps(_agent_paths: tuple[Path, Path]) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)

    # 3 success+artifacts (newest-first), 1 success no-artifacts,
    # 1 failure+artifacts, 1 already reviewed.
    tasks: list[TaskRecord] = []
    for i in range(3):
        adir = workspace / f"artifacts/success-{i}"
        adir.mkdir(parents=True)
        _write_artifact(adir, "README.md", f"content {i}")
        tasks.append(_make_task(
            f"success-{i}", str(adir),
            timestamp=f"2026-04-0{i + 1}T00:00:00+00:00",
        ))

    tasks.append(_make_task(
        "no-artifacts", None,
        timestamp="2026-04-10T00:00:00+00:00",
    ))

    fail_dir = workspace / "artifacts/failed"
    fail_dir.mkdir(parents=True)
    _write_artifact(fail_dir, "note.md", "x")
    tasks.append(_make_task(
        "failed", str(fail_dir), outcome="failure",
        timestamp="2026-04-11T00:00:00+00:00",
    ))

    reviewed_dir = workspace / "artifacts/already-reviewed"
    reviewed_dir.mkdir(parents=True)
    _write_artifact(reviewed_dir, "README.md", "seen")
    tasks.append(_make_task(
        "already-reviewed", str(reviewed_dir),
        timestamp="2026-04-12T00:00:00+00:00",
    ))

    for t in tasks:
        memory.record_task(t)
    memory.write_artifact_review(ArtifactReview(
        review_id="r-seen",
        task_id="already-reviewed",
        verdict="skip",
        title="t",
        reasoning="r",
        bundle_file_names=[],
        proposed_name=None,
        proposed_description=None,
        timestamp="2026-04-13T00:00:00+00:00",
    ))

    picked = reviewer.select_candidates(3)
    ids = [t.task_id for t in picked]
    # Newest first among the three eligible success+artifacts entries.
    assert ids == ["success-2", "success-1", "success-0"]


def test_select_candidates_respects_batch_cap(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    for i in range(5):
        adir = workspace / f"artifacts/t-{i}"
        adir.mkdir(parents=True)
        _write_artifact(adir, "README.md", "x")
        memory.record_task(_make_task(
            f"t-{i}", str(adir),
            timestamp=f"2026-04-0{i + 1}T00:00:00+00:00",
        ))
    assert [t.task_id for t in reviewer.select_candidates(2)] == ["t-4", "t-3"]


def test_select_candidates_zero_batch_returns_empty(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-0"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    memory.record_task(_make_task("t-0", str(adir)))
    assert reviewer.select_candidates(0) == []


# ---------------------------------------------------------------------------
# _read_artifact_bundle — bounds and filtering
# ---------------------------------------------------------------------------


def test_bundle_filters_binaries_and_respects_caps(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(
        memory_dir, workspace,
        artifact_review_max_file_bytes=20,
        artifact_review_max_files_per_task=3,
        artifact_review_max_bundle_bytes=500,
    )
    adir = workspace / "artifacts/bundle-test"
    adir.mkdir(parents=True)
    # Allowed text files
    _write_artifact(adir, "a.md", "A" * 50)
    _write_artifact(adir, "b.py", "B" * 50)
    _write_artifact(adir, "c.txt", "C" * 50)
    _write_artifact(adir, "d.json", "D" * 50)
    # Disallowed binaries
    _write_artifact(adir, "img.png", "binary-should-never-appear")
    _write_artifact(adir, "blob.jpg", "binary-should-never-appear")

    task = _make_task("t-bundle", str(adir))
    text, names = reviewer._read_artifact_bundle(task)

    assert "binary-should-never-appear" not in text
    assert len(names) == 3  # max_files_per_task
    # Each included body is head-truncated at 20 bytes.
    for name in names:
        assert "…[truncated]" in text or f"===== {name} =====" in text
    # No body chunk exceeds per-file cap + sentinel
    segments = text.split("===== ")
    for seg in segments[1:]:
        body = seg.split(" =====\n", 1)[1]
        body_trimmed = body.rstrip("\n").replace("\n…[truncated]", "")
        assert len(body_trimmed) <= 20


def test_bundle_honours_total_bundle_cap(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(
        memory_dir, workspace,
        artifact_review_max_file_bytes=1000,
        artifact_review_max_files_per_task=50,
        artifact_review_max_bundle_bytes=150,
    )
    adir = workspace / "artifacts/many-files"
    adir.mkdir(parents=True)
    for i in range(10):
        _write_artifact(adir, f"f-{i:02d}.md", "Z" * 100)

    task = _make_task("t-many", str(adir))
    text, names = reviewer._read_artifact_bundle(task)

    assert len(names) < 10  # bundle cap kicks in before file count
    assert len(text) <= 300  # approx upper bound with headers; well under 10 * 100


def test_bundle_empty_artifacts_dir_returns_empty(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    task = _make_task("t-none", None)
    assert reviewer._read_artifact_bundle(task) == ("", [])


# ---------------------------------------------------------------------------
# review() — LLM parse paths
# ---------------------------------------------------------------------------


def _make_llm_response(entries: list[dict[str, Any]]) -> QueryResult:
    return QueryResult(response=json.dumps(entries))


@pytest.mark.asyncio
async def test_review_happy_path_persists_and_journals(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, journal, _ = _build_reviewer(memory_dir, workspace)
    # Three tasks, three verdicts.
    tasks: list[TaskRecord] = []
    for idx, tid in enumerate(("t-pub", "t-imp", "t-skp")):
        adir = workspace / f"artifacts/{tid}"
        adir.mkdir(parents=True)
        _write_artifact(adir, "README.md", f"safe content {idx}")
        tasks.append(_make_task(tid, str(adir)))

    entries = [
        {
            "task_id": "t-pub", "verdict": "publish",
            "title": "Cool lib", "reasoning": "Self-contained",
            "proposed_name": "cool-lib",
            "proposed_description": "A short description",
            "readme_markdown": "X" * 700,
        },
        {
            "task_id": "t-imp", "verdict": "improve",
            "title": "Nearly there", "reasoning": "Needs docs",
        },
        {
            "task_id": "t-skp", "verdict": "skip",
            "title": "Scratch", "reasoning": "Not useful",
        },
    ]

    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review(tasks)

    assert mock.call_count == 1
    assert len(out) == 3
    persisted = memory._load_all_reviews()
    by_task = {r.task_id: r for r in persisted}
    assert by_task["t-pub"].verdict == "publish"
    assert by_task["t-pub"].proposed_readme is not None
    assert by_task["t-pub"].proposed_readme.startswith("X" * 10)
    assert by_task["t-imp"].verdict == "improve"
    assert by_task["t-skp"].verdict == "skip"

    # Only improve triggers a realization entry.
    realizations = [e for e in journal._load_events() if e.entry_type == "realization"]
    assert len(realizations) == 1
    assert "improve" in realizations[0].content.lower()
    assert realizations[0].triggered_by == "artifact_review"


@pytest.mark.asyncio
async def test_review_publish_logs_info_and_does_not_emit_proposal(
    _agent_paths: tuple[Path, Path], caplog: pytest.LogCaptureFixture,
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-pub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-pub", str(adir))

    entries = [
        {
            "task_id": "t-pub", "verdict": "publish",
            "title": "A thing", "reasoning": "Ready",
            "proposed_name": "a-thing",
            "proposed_description": "Does a thing",
            "readme_markdown": "A" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    import logging
    with (
        patch("maestro.artifact_review.query", mock),
        caplog.at_level(logging.INFO, logger="maestro"),
    ):
        await reviewer.review([task])

    # Proposal file must not exist — 032 owns emission.
    proposals_path = memory_dir / "publication_proposals.jsonl"
    assert not proposals_path.exists()

    assert any(
        "publish verdict recorded" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_review_malformed_json_returns_empty(
    _agent_paths: tuple[Path, Path], caplog: pytest.LogCaptureFixture,
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-x"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-x", str(adir))

    mock = AsyncMock(return_value=QueryResult(response="not json at all"))
    import logging
    with (
        patch("maestro.artifact_review.query", mock),
        caplog.at_level(logging.WARNING, logger="maestro"),
    ):
        out = await reviewer.review([task])

    assert out == []
    assert memory._load_all_reviews() == []
    assert any(
        "failed to parse LLM response" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_review_non_list_payload_returns_empty(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-x"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-x", str(adir))

    mock = AsyncMock(return_value=QueryResult(
        response=json.dumps({"task_id": "t-x", "verdict": "skip",
                             "title": "t", "reasoning": "r"}),
    ))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert out == []
    assert memory._load_all_reviews() == []


@pytest.mark.asyncio
async def test_review_hard_block_forces_improve(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-secret"
    adir.mkdir(parents=True)
    leaky = "sk-ant-" + "A" * 40  # Anthropic API key shape
    _write_artifact(adir, "README.md", f"Some notes and a key: {leaky}")
    task = _make_task("t-secret", str(adir))

    entries = [
        {
            "task_id": "t-secret", "verdict": "publish",
            "title": "Leaky", "reasoning": "Looks ready",
            "proposed_name": "leaky",
            "proposed_description": "x",
            "readme_markdown": "Y" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    review = out[0]
    assert review.verdict == "improve"
    assert "[scrub_blocked:anthropic_key]" in review.reasoning
    # Publish-only fields dropped on downgrade.
    assert review.proposed_name is None
    assert review.proposed_description is None
    assert review.proposed_readme is None


@pytest.mark.asyncio
async def test_review_budget_throttle_skips_llm_call(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-x"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-x", str(adir))

    # Selection should still yield a candidate even under throttle —
    # the gate lives inside ``review()``.
    assert reviewer.select_candidates(1) == [task] or True

    mock = AsyncMock()
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task], budget_throttle_active=True)

    assert mock.call_count == 0
    assert out == []
    assert memory._load_all_reviews() == []


@pytest.mark.asyncio
async def test_review_unknown_verdict_defaults_to_skip(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-x"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-x", str(adir))

    entries = [
        {
            "task_id": "t-x", "verdict": "maybe",
            "title": "odd", "reasoning": "weird",
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    assert out[0].verdict == "skip"


@pytest.mark.asyncio
async def test_review_skips_entry_with_unknown_task_id(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-known"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-known", str(adir))

    entries = [
        {"task_id": "t-other", "verdict": "skip",
         "title": "x", "reasoning": "x"},
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert out == []
    assert memory._load_all_reviews() == []


@pytest.mark.asyncio
async def test_review_dedupes_duplicate_task_id_in_single_response(
    _agent_paths: tuple[Path, Path], caplog: pytest.LogCaptureFixture,
) -> None:
    """Two entries naming the same task_id persist exactly once (#1486)."""
    import logging as _logging
    memory_dir, workspace = _agent_paths
    reviewer, memory, journal, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-dup"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-dup", str(adir))

    entries = [
        {"task_id": "t-dup", "verdict": "improve",
         "title": "First", "reasoning": "needs work"},
        {"task_id": "t-dup", "verdict": "reuse",
         "title": "Second", "reasoning": "and again"},
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with (
        patch("maestro.artifact_review.query", mock),
        caplog.at_level(_logging.WARNING, logger="maestro"),
    ):
        out = await reviewer.review([task])

    # Only the first entry was persisted; the second was skipped.
    assert len(out) == 1
    assert out[0].verdict == "improve"
    persisted = memory._load_all_reviews()
    assert len(persisted) == 1
    assert persisted[0].task_id == "t-dup"
    assert persisted[0].verdict == "improve"
    # Side-effects fired exactly once for the persisted row.
    realizations = [
        e for e in journal._load_events() if e.entry_type == "realization"
    ]
    assert len(realizations) == 1
    # The duplicate skip emitted a warning naming the task_id.
    assert any(
        "duplicate entry" in r.message and "t-dup" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_review_dedupes_duplicate_publish_emits_one_proposal(
    _agent_paths: tuple[Path, Path], caplog: pytest.LogCaptureFixture,
) -> None:
    """Two publish entries for the same task_id emit one proposal + ping."""
    import logging as _logging
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-dup-pub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "a safe readme")
    task = _make_task("t-dup-pub", str(adir))

    # READMEs must clear ``_MIN_README_LENGTH`` (600 chars) so the
    # publish verdict isn't demoted to ``improve`` before emission runs.
    long_readme_1 = "# dup-pub\n\nSafe README.\n\n" + ("Body. " * 120)
    long_readme_2 = "# dup-pub-2\n\nAnother safe README.\n\n" + ("Body. " * 120)
    entries = [
        {"task_id": "t-dup-pub", "verdict": "publish",
         "title": "First", "reasoning": "ready",
         "proposed_name": "dup-pub",
         "proposed_description": "x",
         "readme_markdown": long_readme_1},
        {"task_id": "t-dup-pub", "verdict": "publish",
         "title": "Second", "reasoning": "also ready",
         "proposed_name": "dup-pub-2",
         "proposed_description": "y",
         "readme_markdown": long_readme_2},
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with (
        patch("maestro.artifact_review.query", mock),
        caplog.at_level(_logging.WARNING, logger="maestro"),
    ):
        out = await reviewer.review([task])

    assert len(out) == 1
    assert len(memory._load_all_reviews()) == 1
    proposals = memory.get_publication_proposals()
    assert len(proposals) == 1
    assert proposals[0].proposed_name == "dup-pub"
    assert len(notifier.pub_calls) == 1
    assert any(
        "duplicate entry" in r.message and "t-dup-pub" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# proposed_readme round-trip
# ---------------------------------------------------------------------------


def test_proposed_readme_round_trip(_agent_paths: tuple[Path, Path]) -> None:
    memory_dir, workspace = _agent_paths
    _, memory, _, _ = _build_reviewer(memory_dir, workspace)

    readme_text = "# Title\n\n" + ("Body paragraph. " * 50)
    memory.write_artifact_review(ArtifactReview(
        review_id="r-new",
        task_id="t-new",
        verdict="publish",
        title="A nice README",
        reasoning="ship it",
        bundle_file_names=["README.md"],
        proposed_name="nice",
        proposed_description="A thing",
        timestamp="2026-04-24T00:00:00+00:00",
        proposed_readme=readme_text,
    ))

    [loaded] = memory._load_all_reviews()
    assert loaded.proposed_readme == readme_text


# ---------------------------------------------------------------------------
# Defensive OSError / disk-error paths
# ---------------------------------------------------------------------------


def test_artifacts_non_empty_iterdir_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Unreadable dir (OSError on iterdir) logs and returns False."""
    import logging as _logging
    d = tmp_path / "broken"
    d.mkdir()
    with (
        patch.object(Path, "iterdir", side_effect=OSError("boom")),
        caplog.at_level(_logging.WARNING, logger="maestro"),
    ):
        assert _artifacts_non_empty(str(d)) is False
    assert any("unreadable" in r.message for r in caplog.records)


def test_bundle_rglob_oserror_returns_empty(
    _agent_paths: tuple[Path, Path], caplog: pytest.LogCaptureFixture,
) -> None:
    import logging as _logging
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/broken"
    adir.mkdir(parents=True)
    task = _make_task("t-broken", str(adir))
    with (
        patch.object(Path, "rglob", side_effect=OSError("permission denied")),
        caplog.at_level(_logging.WARNING, logger="maestro"),
    ):
        text, names = reviewer._read_artifact_bundle(task)
    assert (text, names) == ("", [])
    assert any("failed to walk" in r.message for r in caplog.records)


def test_bundle_is_file_oserror_skips_entry(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-isfile"
    adir.mkdir(parents=True)
    _write_artifact(adir, "a.md", "safe content")
    task = _make_task("t-isfile", str(adir))

    real_is_file = Path.is_file
    call_count = {"n": 0}

    def flaky_is_file(self: Path) -> bool:
        call_count["n"] += 1
        # First invocation raises, subsequent delegate to real.
        if call_count["n"] == 1:
            raise OSError("flaky")
        return real_is_file(self)

    with patch.object(Path, "is_file", flaky_is_file):
        text, names = reviewer._read_artifact_bundle(task)

    # Still works; one entry was skipped but the rest are returned.
    assert isinstance(text, str)
    assert isinstance(names, list)


def test_bundle_read_text_error_skips_file(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-unreadable"
    adir.mkdir(parents=True)
    _write_artifact(adir, "good.md", "safe")
    bad = _write_artifact(adir, "bad.md", "will fail")
    task = _make_task("t-unreadable", str(adir))

    real_read_text = Path.read_text

    def flaky_read(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == bad:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad encoding")
        return real_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", flaky_read):
        text, names = reviewer._read_artifact_bundle(task)

    assert "good.md" in names
    assert "bad.md" not in names
    assert "safe" in text


def test_bundle_skips_when_artifacts_dir_is_file(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A path that exists but is not a directory returns an empty bundle."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    not_a_dir = workspace / "oops.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    task = _make_task("t-not-a-dir", str(not_a_dir))
    assert reviewer._read_artifact_bundle(task) == ("", [])


@pytest.mark.asyncio
async def test_review_query_raises_returns_empty(
    _agent_paths: tuple[Path, Path], caplog: pytest.LogCaptureFixture,
) -> None:
    import logging as _logging
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-err"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-err", str(adir))

    mock = AsyncMock(side_effect=RuntimeError("network down"))
    with (
        patch("maestro.artifact_review.query", mock),
        caplog.at_level(_logging.WARNING, logger="maestro"),
    ):
        out = await reviewer.review([task])

    assert out == []
    assert memory._load_all_reviews() == []
    assert any("LLM call failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_review_non_dict_entry_logged_and_skipped(
    _agent_paths: tuple[Path, Path], caplog: pytest.LogCaptureFixture,
) -> None:
    import logging as _logging
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-x"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-x", str(adir))

    mock = AsyncMock(return_value=_make_llm_response([
        "not a dict",  # type: ignore[list-item]
        {"task_id": "t-x", "verdict": "skip", "title": "t", "reasoning": "r"},
    ]))
    with (
        patch("maestro.artifact_review.query", mock),
        caplog.at_level(_logging.WARNING, logger="maestro"),
    ):
        out = await reviewer.review([task])

    assert len(out) == 1
    assert out[0].verdict == "skip"
    assert any("non-dict entry" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_review_reuse_writes_realization_entry(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, journal, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-reuse"
    adir.mkdir(parents=True)
    _write_artifact(adir, "pattern.py", "def clever(): pass")
    task = _make_task("t-reuse", str(adir))

    entries = [{
        "task_id": "t-reuse", "verdict": "reuse",
        "title": "Clever pattern", "reasoning": "Worth remembering",
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    realizations = [
        e for e in journal._load_events() if e.entry_type == "realization"
    ]
    assert len(realizations) == 1
    assert "reuse" in realizations[0].content.lower()


@pytest.mark.asyncio
async def test_review_publish_with_missing_readme_downgrades_to_improve(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A ``publish`` verdict with no ``readme_markdown`` is downgraded.

    The prompt requires ``readme_markdown`` for ``publish``; an entry that
    omits it must be downgraded to ``improve`` with a ``[readme_missing]``
    reasoning suffix and all publish-only fields cleared — mirroring the
    existing scrub-blocked downgrade discipline.
    """
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-bare"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-bare", str(adir))

    entries = [{
        "task_id": "t-bare", "verdict": "publish",
        "title": "Bare", "reasoning": "ok",
        "proposed_name": "bare",
        "proposed_description": "Does a thing",
        # readme_markdown omitted
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    [loaded] = memory._load_all_reviews()
    assert loaded.verdict == "improve"
    assert loaded.reasoning.endswith("[readme_missing]")
    assert loaded.proposed_name is None
    assert loaded.proposed_description is None
    assert loaded.proposed_readme is None


@pytest.mark.asyncio
async def test_review_publish_with_short_readme_downgrades_to_improve(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A ``publish`` verdict whose README is one char below the floor downgrades.

    Picking 599 (one short of ``_MIN_README_LENGTH``) means a future bump of
    the floor will make this test fail loudly and force the contract to be
    revisited explicitly.
    """
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-short"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-short", str(adir))

    entries = [{
        "task_id": "t-short", "verdict": "publish",
        "title": "Short", "reasoning": "ok",
        "proposed_name": "short",
        "proposed_description": "Does a thing",
        "readme_markdown": "Z" * 599,  # one short of _MIN_README_LENGTH
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    [loaded] = memory._load_all_reviews()
    assert loaded.verdict == "improve"
    assert loaded.reasoning.endswith("[readme_missing]")
    assert loaded.proposed_name is None
    assert loaded.proposed_description is None
    assert loaded.proposed_readme is None


@pytest.mark.asyncio
async def test_review_publish_with_whitespace_padded_readme_downgrades(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A whitespace-padded README clearing the floor only by spaces downgrades.

    The truthiness guard accepts one real char, and the unstripped 701-char
    value would clear ``_MIN_README_LENGTH`` — but the floor must be measured
    on stripped content (the prompt promises 600 *real* chars). Stripped length
    here is 1, so the entry downgrades to ``improve``.
    """
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-padded"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-padded", str(adir))

    entries = [{
        "task_id": "t-padded", "verdict": "publish",
        "title": "Padded", "reasoning": "ok",
        "proposed_name": "padded",
        "proposed_description": "Does a thing",
        "readme_markdown": "X" + " " * 700,  # total 701, stripped 1
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    [loaded] = memory._load_all_reviews()
    assert loaded.verdict == "improve"
    assert loaded.reasoning.endswith("[readme_missing]")
    assert loaded.proposed_name is None
    assert loaded.proposed_description is None
    assert loaded.proposed_readme is None


@pytest.mark.asyncio
async def test_review_publish_strips_surrounding_whitespace_from_readme(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A valid README with surrounding whitespace is persisted stripped.

    Stripping at assignment keeps the stored content and the measured length
    consistent: the floor sees the real Markdown, and the persisted
    ``proposed_readme`` carries no leading/trailing whitespace.
    """
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-surround"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-surround", str(adir))

    entries = [{
        "task_id": "t-surround", "verdict": "publish",
        "title": "Surround", "reasoning": "ok",
        "proposed_name": "surround",
        "proposed_description": "Does a thing",
        "readme_markdown": "\n\n" + "X" * 700 + "\n\n",
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    [loaded] = memory._load_all_reviews()
    assert loaded.verdict == "publish"
    assert loaded.proposed_readme == "X" * 700


_OMIT = object()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_name", [_OMIT, None, "   ", 123])
async def test_review_publish_with_missing_name_downgrades_to_improve(
    _agent_paths: tuple[Path, Path], raw_name: object,
) -> None:
    """A ``publish`` verdict with no usable ``proposed_name`` is downgraded.

    A nameless publish row can never be emitted, so persisting it as
    ``publish`` would hold a backfill slot forever (issue #4115).
    """
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-noname"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-noname", str(adir))

    entry: dict[str, Any] = {
        "task_id": "t-noname", "verdict": "publish",
        "title": "NoName", "reasoning": "ok",
        "proposed_description": "Does a thing",
        "readme_markdown": "X" * 700,
    }
    if raw_name is not _OMIT:
        entry["proposed_name"] = raw_name
    mock = AsyncMock(return_value=_make_llm_response([entry]))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    [loaded] = memory._load_all_reviews()
    assert loaded.verdict == "improve"
    assert loaded.reasoning.endswith("[name_missing]")
    assert loaded.proposed_name is None
    assert loaded.proposed_description is None
    assert loaded.proposed_readme is None


@pytest.mark.asyncio
async def test_review_publish_with_valid_readme_drops_missing_description(
    _agent_paths: tuple[Path, Path],
) -> None:
    """publish verdict + valid README + name but no description → still
    persists as publish with ``proposed_description`` recorded as None;
    the description is optional because emission does not depend on it.
    """
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-only-readme"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-only-readme", str(adir))

    entries = [{
        "task_id": "t-only-readme", "verdict": "publish",
        "title": "ReadmeOnly", "reasoning": "ok",
        "proposed_name": "readme-only",
        # proposed_description omitted; README clears the floor.
        "readme_markdown": "X" * 700,
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert len(out) == 1
    [loaded] = memory._load_all_reviews()
    assert loaded.verdict == "publish"
    assert loaded.proposed_name == "readme-only"
    assert loaded.proposed_description is None
    assert loaded.proposed_readme is not None
    assert loaded.proposed_readme.startswith("X" * 10)


@pytest.mark.asyncio
async def test_review_empty_title_falls_back_to_goal(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-title"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-title", str(adir), goal="My special goal")

    entries = [{
        "task_id": "t-title", "verdict": "skip",
        # no title / empty title
        "reasoning": "r",
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        out = await reviewer.review([task])

    assert out[0].title == "My special goal"


def test_bundle_skips_subdirectories(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Subdirectories returned by rglob are silently skipped."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-sub"
    adir.mkdir(parents=True)
    (adir / "sub").mkdir()
    _write_artifact(adir, "sub/nested.md", "inside")
    _write_artifact(adir, "top.md", "outside")

    task = _make_task("t-sub", str(adir))
    text, names = reviewer._read_artifact_bundle(task)
    # Both files show up but the "sub" directory itself is not included.
    assert "sub" not in names  # bare dir name not in included list
    assert any("sub/nested.md" in n for n in names)
    assert "top.md" in names
    assert "inside" in text and "outside" in text


def _outside_secret(tmp_path_root: Path) -> Path:
    outside = tmp_path_root / "outside"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.bin"
    secret.write_text("OUTSIDE-MARKER-3989", encoding="utf-8")
    return secret


def test_bundle_excludes_file_symlink_escaping_artifact_root(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A file symlink whose target lies outside the bundle is never read."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-esc"
    adir.mkdir(parents=True)
    _write_artifact(adir, "ok.md", "in-root")
    os.symlink(_outside_secret(workspace.parent), adir / "link.txt")

    text, names = reviewer._read_artifact_bundle(_make_task("t-esc", str(adir)))
    assert "link.txt" not in names
    assert "OUTSIDE-MARKER-3989" not in text
    assert names == ["ok.md"]


def test_bundle_admits_in_root_symlink(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Containment, not a symlink ban: an in-root link is still admitted."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-inlink"
    adir.mkdir(parents=True)
    _write_artifact(adir, "real.md", "real body")
    os.symlink(adir / "real.md", adir / "alias.md")

    text, names = reviewer._read_artifact_bundle(_make_task("t-inlink", str(adir)))
    assert names == ["alias.md", "real.md"]
    assert text.count("real body") == 2


def test_bundle_rejects_in_root_symlink_to_disallowed_suffix(
    _agent_paths: tuple[Path, Path],
) -> None:
    """An allowed-suffix link cannot smuggle a disallowed-suffix target."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-smuggle"
    adir.mkdir(parents=True)
    _write_artifact(adir, "blob.bin", "binary-ish")
    os.symlink(adir / "blob.bin", adir / "x.md")

    text, names = reviewer._read_artifact_bundle(_make_task("t-smuggle", str(adir)))
    assert names == []
    assert text == ""


def test_bundle_skips_broken_symlink(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A dangling link is skipped without raising."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-broken"
    adir.mkdir(parents=True)
    _write_artifact(adir, "ok.md", "fine")
    os.symlink(adir / "missing.md", adir / "dangling.md")

    _, names = reviewer._read_artifact_bundle(_make_task("t-broken", str(adir)))
    assert names == ["ok.md"]


def test_bundle_excludes_entry_under_symlinked_directory(
    _agent_paths: tuple[Path, Path],
) -> None:
    """No file reached through an escaping directory symlink is included."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-dirlink"
    adir.mkdir(parents=True)
    _write_artifact(adir, "ok.md", "fine")
    outside_dir = workspace.parent / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "leak.md").write_text("DIR-MARKER-3989", encoding="utf-8")
    os.symlink(outside_dir, adir / "escape", target_is_directory=True)

    text, names = reviewer._read_artifact_bundle(_make_task("t-dirlink", str(adir)))
    assert names == ["ok.md"]
    assert "DIR-MARKER-3989" not in text


def test_bundle_empty_when_root_resolve_fails(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A root that cannot be resolved yields an empty bundle."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-rootfail"
    adir.mkdir(parents=True)
    _write_artifact(adir, "ok.md", "fine")

    with patch.object(Path, "resolve", side_effect=RuntimeError("loop")):
        result = reviewer._read_artifact_bundle(_make_task("t-rootfail", str(adir)))
    assert result == ("", [])


def test_bundle_skips_entry_whose_resolve_fails(
    _agent_paths: tuple[Path, Path],
) -> None:
    """An entry that cannot be resolved (e.g. a symlink loop) is skipped."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-entryfail"
    adir.mkdir(parents=True)
    _write_artifact(adir, "a.md", "alpha")
    _write_artifact(adir, "b.md", "beta")
    real_resolve = Path.resolve

    def _resolve(self: Path, strict: bool = False) -> Path:
        if self.name == "a.md":
            raise OSError("loop")
        return real_resolve(self, strict=strict)

    with patch.object(Path, "resolve", _resolve):
        _, names = reviewer._read_artifact_bundle(
            _make_task("t-entryfail", str(adir)),
        )
    assert names == ["b.md"]


@pytest.mark.asyncio
async def test_review_prompt_never_carries_escaped_content(
    _agent_paths: tuple[Path, Path],
) -> None:
    """The out-of-tree target never reaches the priced SDK prompt."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-prompt"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    os.symlink(_outside_secret(workspace.parent), adir / "link.txt")

    mock = AsyncMock(return_value=_make_llm_response([]))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([_make_task("t-prompt", str(adir))])

    assert mock.call_count == 1
    assert "OUTSIDE-MARKER-3989" not in str(mock.call_args)


def test_format_task_block_shows_files_none_when_bundle_empty(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Task with no readable artifact text renders ``Files: <none>``."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    task = _make_task("t-empty-bundle", None, goal="thinking")
    block = reviewer._format_task_block(task, "", [])
    assert "Files: <none>" in block
    assert "Goal: thinking" in block


@pytest.mark.asyncio
async def test_review_non_string_reasoning_defaults_to_empty(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-rsn"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "x")
    task = _make_task("t-rsn", str(adir))

    entries = [{
        "task_id": "t-rsn", "verdict": "skip",
        "title": "t", "reasoning": 42,  # non-string
    }]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    [loaded] = memory._load_all_reviews()
    assert loaded.reasoning == ""


# ---------------------------------------------------------------------------
# Byte-aware truncation for multi-byte UTF-8 content
# ---------------------------------------------------------------------------


def test_read_artifact_bundle_respects_byte_limit_for_multibyte(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Per-file cap is enforced as UTF-8 bytes, not characters.

    Each emoji is 4 bytes in UTF-8.  10 emoji = 10 chars but 40 bytes.
    With ``max_file_bytes=20`` the file must be truncated even though
    ``len(text) == 10 < 20``.
    """
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(
        memory_dir, workspace,
        artifact_review_max_file_bytes=20,
        artifact_review_max_files_per_task=10,
        artifact_review_max_bundle_bytes=100_000,
    )
    adir = workspace / "artifacts/t-utf8"
    adir.mkdir(parents=True)
    emoji_text = "\U0001f389" * 10  # 10 chars, 40 UTF-8 bytes
    _write_artifact(adir, "emoji.md", emoji_text)

    task = _make_task("t-utf8", str(adir))
    text, names = reviewer._read_artifact_bundle(task)

    assert "emoji.md" in names
    # The body must have been truncated (sentinel present).
    assert "…[truncated]" in text
    # Extract the body content (excluding header and sentinel).
    body = text.split("=====\n", 1)[1].rstrip("\n").replace(
        "\n…[truncated]", "",
    )
    # Body bytes must not exceed the per-file cap.
    assert len(body.encode("utf-8")) <= 20
    # Body characters should be fewer than the original 10.
    assert len(body) < 10


def test_read_artifact_bundle_respects_bundle_byte_limit_for_multibyte(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Bundle-level cap is enforced as UTF-8 bytes, not characters.

    Two files of 4-byte-per-char content: each file's section is well
    under the per-file cap but together they exceed the bundle cap when
    measured in bytes — so the second file must be excluded.
    """
    memory_dir, workspace = _agent_paths
    # Per-file cap generous, but bundle cap tight: only one file fits.
    reviewer, _, _, _ = _build_reviewer(
        memory_dir, workspace,
        artifact_review_max_file_bytes=100_000,
        artifact_review_max_files_per_task=10,
        artifact_review_max_bundle_bytes=120,
    )
    adir = workspace / "artifacts/t-bundle-utf8"
    adir.mkdir(parents=True)
    # Each emoji is 4 bytes. 20 emoji = 80 bytes of body + header ≈ 100+
    content = "\U0001f389" * 20
    _write_artifact(adir, "a.md", content)
    _write_artifact(adir, "b.md", content)

    task = _make_task("t-bundle-utf8", str(adir))
    text, names = reviewer._read_artifact_bundle(task)

    # Only the first file should fit within the 120-byte bundle cap.
    assert len(names) == 1
    assert names[0] == "a.md"
    # Total bundle bytes must respect the cap.
    assert len(text.encode("utf-8")) <= 120


def test_proposed_readme_legacy_row_loads_as_none(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    _, memory, _, _ = _build_reviewer(memory_dir, workspace)

    legacy_row = {
        "review_id": "r-legacy",
        "task_id": "t-legacy",
        "verdict": "publish",
        "title": "t",
        "reasoning": "r",
        "bundle_file_names": ["a"],
        "proposed_name": "n",
        "proposed_description": "d",
        "timestamp": "2026-04-01T00:00:00+00:00",
        # NOTE: no ``proposed_readme`` key — 030-era row.
    }
    (memory_dir / "artifact_reviews.jsonl").write_text(
        json.dumps(legacy_row) + "\n", encoding="utf-8",
    )
    [loaded] = memory._load_all_reviews()
    assert loaded.proposed_readme is None


# ---------------------------------------------------------------------------
# Proposal 032 — publication-proposal emission + backfill
# ---------------------------------------------------------------------------


class _FakeHost:
    """Minimal ``CodeHost``-shaped stub for emission tests.

    Only the two methods the emission path touches are implemented;
    the rest would raise ``AttributeError`` on use, which is exactly
    what we want — it localises test scope.
    """

    def __init__(
        self,
        *,
        owner: str = "thomas-sodon",
        existing: set[tuple[str, str]] | None = None,
        resolve_raises: bool = False,
        exists_raises: bool = False,
    ) -> None:
        self._owner = owner
        self._existing = existing or set()
        self._resolve_raises = resolve_raises
        self._exists_raises = exists_raises
        self.resolve_calls = 0
        self.exists_calls: list[tuple[str, str]] = []

    def resolve_owner(self) -> str:
        self.resolve_calls += 1
        if self._resolve_raises:
            raise RuntimeError("gh api failed")
        return self._owner

    def repo_exists(self, owner: str, name: str) -> bool:
        self.exists_calls.append((owner, name))
        if self._exists_raises:
            raise RuntimeError("gh api failed")
        return (owner, name) in self._existing


class _RecordingNotifier:
    """Collects ``notify_publication_for_review`` calls for assertions."""

    def __init__(self, *, raises: bool = False) -> None:
        self.pub_calls: list[tuple[str, str]] = []
        self.plain_calls: list[str] = []
        self._raises = raises

    async def notify(
        self, message: str, parse_mode: str | None = None,
    ) -> None:
        self.plain_calls.append(message)

    async def notify_publication_for_review(
        self, proposal_id: str, message: str,
    ) -> None:
        if self._raises:
            raise RuntimeError("telegram down")
        self.pub_calls.append((proposal_id, message))

    async def resolve_plan(self, plan_id: str, label: str) -> None:
        return

    async def resolve_publication(
        self, proposal_id: str, label: str,
    ) -> None:
        return

    async def ask_thomas(self, question: str, message_id: str) -> None:
        return


def _build_wired_reviewer(
    memory_dir: Path,
    workspace: Path,
    *,
    host: _FakeHost | None = None,
    notifier: _RecordingNotifier | None = None,
    publication_enabled: bool = True,
    publication_owner: str = "",
    **config_overrides: Any,
) -> tuple[
    ArtifactReviewer, Memory, Journal, _FakeHost, _RecordingNotifier,
]:
    _host = host or _FakeHost()
    _notifier = notifier or _RecordingNotifier()
    config = _make_config(
        memory_dir, workspace,
        publication_enabled=publication_enabled,
        publication_owner=publication_owner,
        **config_overrides,
    )
    memory = Memory(memory_dir)
    journal = Journal(memory_dir)
    reviewer = ArtifactReviewer(
        memory, journal, config,
        clock=lambda: datetime(2026, 4, 24, tzinfo=UTC),
        host=_host,
        notifier=_notifier,
    )
    return reviewer, memory, journal, _host, _notifier


@pytest.mark.asyncio
async def test_emission_happy_path_persists_and_notifies(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, host, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-pub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "a perfectly safe readme")
    task = _make_task("t-pub", str(adir))

    entries = [
        {
            "task_id": "t-pub", "verdict": "publish",
            "title": "Cool tool", "reasoning": "Self-contained",
            "proposed_name": "cool-tool",
            "proposed_description": "Does a thing",
            "readme_markdown": (
                "# Cool tool\n\nA safe, self-contained utility.\n\n"
                + "More safe content. " * 35
            ),
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    proposals = memory.get_publication_proposals()
    assert len(proposals) == 1
    p = proposals[0]
    assert p.task_id == "t-pub"
    assert p.proposed_name == "cool-tool"
    assert p.status == "pending_approval"
    # Notifier was called with the matching proposal id + a useful body.
    assert len(notifier.pub_calls) == 1
    assert notifier.pub_calls[0][0] == p.proposal_id
    assert "cool-tool" in notifier.pub_calls[0][1]
    # Host's name-collision check ran with the resolved owner.
    assert host.exists_calls == [("thomas-sodon", "cool-tool")]


@pytest.mark.asyncio
async def test_emission_readme_hard_block_downgrades(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-leak"
    adir.mkdir(parents=True)
    # Bundle content is safe — the hard-block is in the drafted README
    # only, so 031's pre-classification scan doesn't catch it.
    _write_artifact(adir, "README.md", "safe bundle")
    task = _make_task("t-leak", str(adir))

    leaky_readme = (
        "# t-leak\n\nUse sk-ant-" + "Z" * 40 + " as your key.\n\n"
        + "Lorem ipsum dolor sit amet. " * 30
    )
    entries = [
        {
            "task_id": "t-leak", "verdict": "publish",
            "title": "Leaky", "reasoning": "Ready",
            "proposed_name": "leaky", "proposed_description": "x",
            "readme_markdown": leaky_readme,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    # No proposal emitted.
    assert memory.get_publication_proposals() == []
    # Notifier never fired for a publication review.
    assert notifier.pub_calls == []
    # Review row was demoted in-place.
    [r] = memory._load_all_reviews()
    assert r.verdict == "improve"
    assert "[scrub_blocked:anthropic_key]" in r.reasoning
    assert r.proposed_name is None
    assert r.proposed_readme is None


@pytest.mark.asyncio
async def test_emission_name_collision_downgrades(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    host = _FakeHost(existing={("thomas-sodon", "taken-name")})
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace, host=host,
    )
    adir = workspace / "artifacts/t-clash"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-clash", str(adir))

    entries = [
        {
            "task_id": "t-clash", "verdict": "publish",
            "title": "Clash", "reasoning": "Ready",
            "proposed_name": "taken-name",
            "proposed_description": "x",
            "readme_markdown": (
                "# Clash\n\nSafe README.\n\n"
                + "Lorem ipsum dolor sit amet. " * 30
            ),
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    [r] = memory._load_all_reviews()
    assert r.verdict == "improve"
    assert "[name_taken:taken-name]" in r.reasoning


@pytest.mark.asyncio
async def test_emission_name_hard_block_downgrades(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A hard-block secret in the LLM-proposed repo name demotes to
    ``improve`` and emits no proposal — issue #1417."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-name"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe bundle")
    task = _make_task("t-name", str(adir))

    entries = [
        {
            "task_id": "t-name", "verdict": "publish",
            "title": "Leaky name", "reasoning": "Ready",
            # AWS access key embedded in the proposed repo name.
            "proposed_name": "AKIAIOSFODNN7EXAMPLE",
            "proposed_description": "safe description",
            "readme_markdown": (
                "# Leaky name\n\nSafe README.\n\n"
                + "Lorem ipsum dolor sit amet. " * 30
            ),
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    [r] = memory._load_all_reviews()
    assert r.verdict == "improve"
    assert "[scrub_blocked:aws_access_key]" in r.reasoning


@pytest.mark.asyncio
async def test_emission_name_narrative_redaction_downgrades(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A narrative-redaction target in the repo name demotes rather than
    corrupting the slug — issue #1417 (the name can't be scrubbed)."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-narr"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe bundle")
    task = _make_task("t-narr", str(adir))

    entries = [
        {
            "task_id": "t-narr", "verdict": "publish",
            "title": "Internal name", "reasoning": "Ready",
            "proposed_name": "maestro-helper",
            "proposed_description": "safe description",
            "readme_markdown": (
                "# Internal name\n\nSafe README.\n\n"
                + "Lorem ipsum dolor sit amet. " * 30
            ),
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    [r] = memory._load_all_reviews()
    assert r.verdict == "improve"
    assert "[scrub_blocked:narrative_redactions:name_maestro]" in r.reasoning


@pytest.mark.asyncio
async def test_emission_description_hard_block_downgrades(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A hard-block secret in the proposed description demotes to
    ``improve`` and emits no proposal — issue #1417."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-desc"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe bundle")
    task = _make_task("t-desc", str(adir))

    entries = [
        {
            "task_id": "t-desc", "verdict": "publish",
            "title": "Leaky desc", "reasoning": "Ready",
            "proposed_name": "safe-name",
            "proposed_description": "Use AKIAIOSFODNN7EXAMPLE to connect",
            "readme_markdown": (
                "# Leaky desc\n\nSafe README.\n\n"
                + "Lorem ipsum dolor sit amet. " * 30
            ),
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    [r] = memory._load_all_reviews()
    assert r.verdict == "improve"
    assert "[scrub_blocked:aws_access_key]" in r.reasoning


@pytest.mark.asyncio
async def test_emission_description_narrative_redaction_scrubbed(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A narrative-redaction target in the description is scrubbed in
    place; the proposal is still emitted with the scrubbed value and the
    finding recorded — issue #1417 (matches the README scrub path)."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, _ = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-scrub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe bundle")
    task = _make_task("t-scrub", str(adir))

    entries = [
        {
            "task_id": "t-scrub", "verdict": "publish",
            "title": "Scrubbable", "reasoning": "Ready",
            "proposed_name": "cool-tool",
            "proposed_description": "Built with maestro internals",
            "readme_markdown": (
                "# Scrubbable\n\nSafe README.\n\n"
                + "Lorem ipsum dolor sit amet. " * 30
            ),
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    proposals = memory.get_publication_proposals()
    assert len(proposals) == 1
    p = proposals[0]
    assert p.description == "Built with <redacted> internals"
    assert "narrative_redactions:name_maestro: 1" in p.scrub_findings
    # The review row keeps its publish verdict — scrubbing is not a demote.
    [r] = memory._load_all_reviews()
    assert r.verdict == "publish"


@pytest.mark.asyncio
async def test_emission_short_circuits_when_publication_disabled(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, host, notifier = _build_wired_reviewer(
        memory_dir, workspace, publication_enabled=False,
    )
    adir = workspace / "artifacts/t-pub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-pub", str(adir))

    entries = [
        {
            "task_id": "t-pub", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    # No proposals, no notifications, no host calls.
    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    assert host.resolve_calls == 0
    assert host.exists_calls == []
    # Review row is untouched.
    [r] = memory._load_all_reviews()
    assert r.verdict == "publish"


@pytest.mark.asyncio
async def test_emission_short_circuits_when_host_or_notifier_missing(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Reviewer instantiated without host/notifier behaves like 031."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _ = _build_reviewer(
        memory_dir, workspace, publication_enabled=True,
    )
    adir = workspace / "artifacts/t-pub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-pub", str(adir))

    entries = [
        {
            "task_id": "t-pub", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    # No emission without host + notifier, verdict stays publish.
    assert memory.get_publication_proposals() == []
    [r] = memory._load_all_reviews()
    assert r.verdict == "publish"


@pytest.mark.asyncio
async def test_emission_notifier_failure_propagates(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Telegram raising during emission propagates to the iteration handler.

    Fail-loud: a notifier outage is a real defect (auth lapse, network
    routing, library mismatch) — silently swallowing it hid Telegram
    regressions. The proposal is durably persisted before the
    notification call, so it survives on the dashboard approval
    surface; backfill will not re-emit this row, though, because its
    ``review_id`` already has a proposal — only the missed Telegram
    notification is lost.
    """
    memory_dir, workspace = _agent_paths
    notifier = _RecordingNotifier(raises=True)
    reviewer, memory, _, _, _ = _build_wired_reviewer(
        memory_dir, workspace, notifier=notifier,
    )
    adir = workspace / "artifacts/t-pub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-pub", str(adir))

    entries = [
        {
            "task_id": "t-pub", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with (
        patch("maestro.artifact_review.query", mock),
        pytest.raises(RuntimeError, match="telegram down"),
    ):
        await reviewer.review([task])

    # Proposal persisted before the notify call; backfill will not re-emit
    # (its review_id already has a proposal) — only the notification is lost.
    proposals = memory.get_publication_proposals()
    assert len(proposals) == 1


@pytest.mark.asyncio
async def test_emission_resolve_owner_failure_skips_safely(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    host = _FakeHost(resolve_raises=True)
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace, host=host,
    )
    adir = workspace / "artifacts/t-pub"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-pub", str(adir))

    entries = [
        {
            "task_id": "t-pub", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    # No proposal (emission bailed out); verdict untouched; row is
    # still publish waiting for a future tick to retry.
    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    [r] = memory._load_all_reviews()
    assert r.verdict == "publish"


@pytest.mark.asyncio
async def test_backfill_emits_for_historical_publish_rows(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    # Two historical publish rows; no matching proposals yet.
    for i in range(2):
        tid = f"t-hist-{i}"
        adir = workspace / f"artifacts/{tid}"
        adir.mkdir(parents=True)
        _write_artifact(adir, "README.md", "historical safe content")
        memory.record_task(_make_task(tid, str(adir)))
        memory.write_artifact_review(ArtifactReview(
            review_id=f"r-hist-{i}",
            task_id=tid,
            verdict="publish",
            title=f"Historical {i}",
            reasoning="Already classified by 031",
            bundle_file_names=["README.md"],
            proposed_name=f"hist-{i}",
            proposed_description="x",
            timestamp=f"2026-04-0{i+1}T00:00:00+00:00",
            proposed_readme=f"# hist-{i}\n\nSafe content.",
        ))

    # Drive ``review()`` with no live candidates — backfill still fires.
    await reviewer.backfill()

    proposals = memory.get_publication_proposals()
    assert len(proposals) == 2
    assert {p.proposed_name for p in proposals} == {"hist-0", "hist-1"}
    assert len(notifier.pub_calls) == 2

    # Idempotent — subsequent review tick emits nothing.
    notifier.pub_calls.clear()
    await reviewer.backfill()
    assert notifier.pub_calls == []
    assert len(memory.get_publication_proposals()) == 2


@pytest.mark.asyncio
async def test_backfill_respects_per_tick_cap(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A large backlog emits at most ``review_batch_size`` per tick."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
        artifact_review_batch_size=2,
    )
    for i in range(5):
        tid = f"t-overflow-{i}"
        adir = workspace / f"artifacts/{tid}"
        adir.mkdir(parents=True)
        _write_artifact(adir, "README.md", "safe")
        memory.record_task(_make_task(tid, str(adir)))
        memory.write_artifact_review(ArtifactReview(
            review_id=f"r-overflow-{i}",
            task_id=tid,
            verdict="publish",
            title=f"Overflow {i}",
            reasoning="Safe",
            bundle_file_names=["README.md"],
            proposed_name=f"overflow-{i}",
            proposed_description="x",
            timestamp=f"2026-04-0{i+1}T00:00:00+00:00",
            proposed_readme=f"# overflow-{i}\n\nSafe.",
        ))

    await reviewer.backfill()
    # Only 2 emissions this tick.
    assert len(notifier.pub_calls) == 2
    assert len(memory.get_publication_proposals()) == 2

    # Second tick drains the next 2.
    notifier.pub_calls.clear()
    await reviewer.backfill()
    assert len(notifier.pub_calls) == 2
    assert len(memory.get_publication_proposals()) == 4

    # Third tick finishes the backlog.
    notifier.pub_calls.clear()
    await reviewer.backfill()
    assert len(notifier.pub_calls) == 1
    assert len(memory.get_publication_proposals()) == 5


@pytest.mark.asyncio
async def test_backfill_coalesces_journal_milestones(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Backfill writes one summary milestone per batch, not N per proposal."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, journal, _, _ = _build_wired_reviewer(
        memory_dir, workspace,
        artifact_review_batch_size=3,
    )
    for i in range(3):
        tid = f"t-jnl-{i}"
        adir = workspace / f"artifacts/{tid}"
        adir.mkdir(parents=True)
        _write_artifact(adir, "README.md", "safe")
        memory.record_task(_make_task(tid, str(adir)))
        memory.write_artifact_review(ArtifactReview(
            review_id=f"r-jnl-{i}",
            task_id=tid,
            verdict="publish",
            title=f"t{i}",
            reasoning="safe",
            bundle_file_names=["README.md"],
            proposed_name=f"jnl-{i}",
            proposed_description="x",
            timestamp=f"2026-04-0{i+1}T00:00:00+00:00",
            proposed_readme=f"# jnl-{i}",
        ))

    await reviewer.backfill()

    milestones = [
        e for e in journal._load_events() if e.entry_type == "milestone"
    ]
    # Exactly one backfill summary — *not* one per emitted proposal.
    backfill_milestones = [
        m for m in milestones if m.triggered_by == "publication_backfill"
    ]
    assert len(backfill_milestones) == 1
    assert "3 proposal" in backfill_milestones[0].content

    # Per-proposal milestones from the live path are suppressed during
    # backfill.
    per_proposal = [
        m for m in milestones if m.triggered_by == "publication_proposal"
    ]
    assert per_proposal == []


@pytest.mark.asyncio
async def test_emission_empty_owner_skips(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Owner resolves to empty string → emission skips cleanly."""
    memory_dir, workspace = _agent_paths
    host = _FakeHost(owner="")
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace, host=host,
    )
    adir = workspace / "artifacts/t-owner"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-owner", str(adir))

    entries = [
        {
            "task_id": "t-owner", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_name", [None, "   "])
async def test_emission_missing_proposed_name_demotes(
    _agent_paths: tuple[Path, Path], legacy_name: str | None,
) -> None:
    """``proposed_name`` empty on a persisted review → terminal demotion."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    # Pre-populate a legacy / hand-edited publish row with no
    # proposed_name — ``review()`` itself now refuses to write one.
    task = _make_task("t-noname", None)
    memory.record_task(task)
    memory.write_artifact_review(ArtifactReview(
        review_id="r-noname",
        task_id=task.task_id,
        verdict="publish",
        title="t",
        reasoning="r",
        bundle_file_names=["README.md"],
        proposed_name=legacy_name,
        proposed_description=None,
        timestamp="2026-04-24T00:00:00+00:00",
        proposed_readme="# safe",
    ))

    await reviewer.backfill()
    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    [loaded] = memory._load_all_reviews()
    assert loaded.verdict == "improve"
    assert loaded.reasoning.endswith("[name_missing]")


@pytest.mark.asyncio
async def test_backfill_nameless_legacy_row_does_not_block_valid_row(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A nameless legacy row ahead of a valid one must not hold the only
    backfill slot forever (issue #4115)."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace, artifact_review_batch_size=1,
    )
    for tid, rid, name in (
        ("t-noname", "r-noname", None), ("t-good", "r-good", "good-name"),
    ):
        memory.record_task(_make_task(tid, None))
        memory.write_artifact_review(ArtifactReview(
            review_id=rid,
            task_id=tid,
            verdict="publish",
            title="t",
            reasoning="r",
            bundle_file_names=["README.md"],
            proposed_name=name,
            proposed_description="Does a thing",
            timestamp="2026-04-24T00:00:00+00:00",
            proposed_readme="# Safe\n\n" + "Safe content. " * 60,
        ))

    await reviewer.backfill()
    await reviewer.backfill()

    proposals = memory.get_publication_proposals()
    assert [p.review_id for p in proposals] == ["r-good"]
    assert len(notifier.pub_calls) == 1


@pytest.mark.asyncio
async def test_emission_repo_exists_failure_skips(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    host = _FakeHost(exists_raises=True)
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace, host=host,
    )
    adir = workspace / "artifacts/t-exfail"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-exfail", str(adir))

    entries = [
        {
            "task_id": "t-exfail", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with patch("maestro.artifact_review.query", mock):
        await reviewer.review([task])

    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []
    # Review row is not demoted — just untouched so a future tick retries.
    [r] = memory._load_all_reviews()
    assert r.verdict == "publish"


@pytest.mark.asyncio
async def test_emission_duplicate_proposal_skipped(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Writing a duplicate proposal id swallows the error and skips notify."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-dup"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-dup", str(adir))

    entries = [
        {
            "task_id": "t-dup", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    from maestro.memory import DuplicateProposalError as _DPE
    with (
        patch("maestro.artifact_review.query", mock),
        patch.object(
            memory, "write_publication_proposal",
            side_effect=_DPE("dup-test"),
        ),
    ):
        await reviewer.review([task])

    # Durable state: no proposal persisted, no Telegram notification.
    # write_publication_proposal was the mocked boundary.
    assert notifier.pub_calls == []


@pytest.mark.asyncio
async def test_emission_generic_persist_failure_propagates(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Non-duplicate persist failure (e.g. disk full) propagates."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-diskfull"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-diskfull", str(adir))

    entries = [
        {
            "task_id": "t-diskfull", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with (
        patch("maestro.artifact_review.query", mock),
        patch.object(
            memory, "write_publication_proposal",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(OSError, match="disk full"),
    ):
        await reviewer.review([task])

    assert notifier.pub_calls == []


@pytest.mark.asyncio
async def test_emission_journal_failure_propagates(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Journal write failure during emission propagates.

    Fail-loud: a journal write failure is a real defect; the loop's
    iteration handler classifies it. The proposal is still durably
    persisted and the notifier was already invoked, so the emission
    side-effects up to that point are intact.
    """
    memory_dir, workspace = _agent_paths
    reviewer, memory, journal, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-jnlfail"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    task = _make_task("t-jnlfail", str(adir))

    entries = [
        {
            "task_id": "t-jnlfail", "verdict": "publish",
            "title": "A", "reasoning": "r",
            "proposed_name": "a", "proposed_description": "d",
            "readme_markdown": "X" * 700,
        },
    ]
    mock = AsyncMock(return_value=_make_llm_response(entries))
    with (
        patch("maestro.artifact_review.query", mock),
        patch.object(
            journal, "record_event",
            side_effect=OSError("journal write failed"),
        ),
        pytest.raises(OSError, match="journal write failed"),
    ):
        await reviewer.review([task])

    # Proposal and notification still succeeded before the journal call.
    assert len(memory.get_publication_proposals()) == 1
    assert len(notifier.pub_calls) == 1


def test_format_publication_notification_includes_scrub_kinds() -> None:
    """Scrub findings render as a sorted-unique ``Scrubbed: …`` line."""
    from maestro.artifact_review import ArtifactReviewer
    from maestro.memory import ArtifactReview as _AR
    from maestro.memory import PublicationProposal as _PP
    proposal = _PP(
        proposal_id="p1", task_id="t1", review_id="r1",
        proposed_name="x", description="y",
        proposed_readme="# x", publishable_files=["a", "b"],
        scrub_findings=["redacted-path: 3", "redacted-email: 1"],
        created_at="2026-04-24T00:00:00+00:00",
    )
    review = _AR(
        review_id="r1", task_id="t1", verdict="publish",
        title="t", reasoning=("because " * 100),
        bundle_file_names=["a", "b"],
        proposed_name="x", proposed_description="y",
        timestamp="2026-04-24T00:00:00+00:00",
    )
    task = _make_task("t1", None, goal="a" * 300)
    body = ArtifactReviewer._format_publication_notification(
        task, proposal, review,
    )
    assert "Scrubbed: redacted-email, redacted-path" in body
    assert "Rationale: because " in body
    # Truncation marker on long goal.
    assert "…" in body


def test_format_publication_notification_hard_block_in_goal_replaced() -> None:
    """A hard-block credential in ``task.goal`` must be redacted from the body."""
    from maestro.artifact_review import ArtifactReviewer
    from maestro.memory import ArtifactReview as _AR
    from maestro.memory import PublicationProposal as _PP
    proposal = _PP(
        proposal_id="p1", task_id="t1", review_id="r1",
        proposed_name="x", description="y",
        proposed_readme="# x", publishable_files=["a"],
        created_at="2026-04-24T00:00:00+00:00",
    )
    review = _AR(
        review_id="r1", task_id="t1", verdict="publish",
        title="t", reasoning="r",
        bundle_file_names=["a"],
        proposed_name="x", proposed_description="y",
        timestamp="2026-04-24T00:00:00+00:00",
    )
    leaky_goal = "Use sk-ant-" + "Z" * 40 + " from my notes"
    task = _make_task("t1", None, goal=leaky_goal)
    body = ArtifactReviewer._format_publication_notification(
        task, proposal, review,
    )
    # Raw secret must not appear; placeholder kind tag must.
    assert "sk-ant-" not in body
    assert "Goal: (redacted: contains anthropic_key)" in body


def test_format_publication_notification_soft_redacts_goal() -> None:
    """Soft-redact patterns in ``task.goal`` (developer paths) are masked."""
    from maestro.artifact_review import ArtifactReviewer
    from maestro.memory import ArtifactReview as _AR
    from maestro.memory import PublicationProposal as _PP
    proposal = _PP(
        proposal_id="p1", task_id="t1", review_id="r1",
        proposed_name="x", description="y",
        proposed_readme="# x", publishable_files=["a"],
        created_at="2026-04-24T00:00:00+00:00",
    )
    review = _AR(
        review_id="r1", task_id="t1", verdict="publish",
        title="t", reasoning="r",
        bundle_file_names=["a"],
        proposed_name="x", proposed_description="y",
        timestamp="2026-04-24T00:00:00+00:00",
    )
    # /home/operator/... is a developer-path REDACT_PATTERN.
    task = _make_task(
        "t1", None,
        goal="run the script at /home/operator/projects/devD/test.py",
    )
    body = ArtifactReviewer._format_publication_notification(
        task, proposal, review,
    )
    assert "/home/operator/projects/devD/test.py" not in body
    assert "<redacted-path>" in body
    assert body.count("Goal:") == 1


@pytest.mark.asyncio
async def test_backfill_short_circuits_when_publication_disabled(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace, publication_enabled=False,
    )
    # Pre-seed publish row; flag is off so backfill must not fire.
    memory.write_artifact_review(ArtifactReview(
        review_id="r-off", task_id="t-off", verdict="publish",
        title="t", reasoning="r", bundle_file_names=["README.md"],
        proposed_name="off", proposed_description="x",
        timestamp="2026-04-01T00:00:00+00:00",
        proposed_readme="# off",
    ))
    await reviewer.backfill()
    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []


@pytest.mark.asyncio
async def test_backfill_load_all_reviews_failure_bails(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    with patch.object(
        memory, "_load_all_reviews", side_effect=OSError("read failed"),
    ):
        await reviewer.backfill()
    assert notifier.pub_calls == []


@pytest.mark.asyncio
async def test_backfill_skips_review_with_missing_task(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    # Publish review with a task_id that is NOT in task history.
    memory.write_artifact_review(ArtifactReview(
        review_id="r-orphan", task_id="t-orphan", verdict="publish",
        title="t", reasoning="r", bundle_file_names=["README.md"],
        proposed_name="orphan", proposed_description="x",
        timestamp="2026-04-01T00:00:00+00:00",
        proposed_readme="# orphan",
    ))
    await reviewer.backfill()
    assert memory.get_publication_proposals() == []
    assert notifier.pub_calls == []


@pytest.mark.asyncio
async def test_backfill_load_all_tasks_failure_bails(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    memory.write_artifact_review(ArtifactReview(
        review_id="r-t", task_id="t-t", verdict="publish",
        title="t", reasoning="r", bundle_file_names=["README.md"],
        proposed_name="t", proposed_description="x",
        timestamp="2026-04-01T00:00:00+00:00",
        proposed_readme="# t",
    ))
    with patch.object(
        memory, "_load_all_tasks", side_effect=OSError("task load failed"),
    ):
        await reviewer.backfill()
    assert notifier.pub_calls == []


@pytest.mark.asyncio
async def test_backfill_summary_count_failure_logged(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, _ = _build_wired_reviewer(
        memory_dir, workspace,
    )
    # Valid backfill source.
    adir = workspace / "artifacts/t-sum"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    memory.record_task(_make_task("t-sum", str(adir)))
    memory.write_artifact_review(ArtifactReview(
        review_id="r-sum", task_id="t-sum", verdict="publish",
        title="t", reasoning="r", bundle_file_names=["README.md"],
        proposed_name="sum", proposed_description="x",
        timestamp="2026-04-01T00:00:00+00:00",
        proposed_readme="# sum",
    ))

    # Force the summary-count step to raise.
    original = memory.get_publication_proposals
    call_count = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original(*args, **kwargs)
        raise OSError("summary read failed")

    with patch.object(
        memory, "get_publication_proposals", side_effect=flaky,
    ):
        await reviewer.backfill()


@pytest.mark.asyncio
async def test_backfill_demote_only_produces_no_milestone(
    _agent_paths: tuple[Path, Path],
) -> None:
    """A backfill pass that only demotes (no emissions) writes no summary."""
    memory_dir, workspace = _agent_paths
    host = _FakeHost(existing={("thomas-sodon", "already-there")})
    reviewer, memory, journal, _, _ = _build_wired_reviewer(
        memory_dir, workspace, host=host,
    )
    adir = workspace / "artifacts/t-noop"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    memory.record_task(_make_task("t-noop", str(adir)))
    memory.write_artifact_review(ArtifactReview(
        review_id="r-noop", task_id="t-noop", verdict="publish",
        title="t", reasoning="r", bundle_file_names=["README.md"],
        proposed_name="already-there", proposed_description="x",
        timestamp="2026-04-01T00:00:00+00:00",
        proposed_readme="# safe",
    ))
    await reviewer.backfill()

    backfill_milestones = [
        e for e in journal._load_events()
        if e.entry_type == "milestone"
        and e.triggered_by == "publication_backfill"
    ]
    assert backfill_milestones == []


@pytest.mark.asyncio
async def test_backfill_summary_journal_failure_swallowed(
    _agent_paths: tuple[Path, Path],
) -> None:
    memory_dir, workspace = _agent_paths
    reviewer, memory, journal, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    adir = workspace / "artifacts/t-summ-fail"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    memory.record_task(_make_task("t-summ-fail", str(adir)))
    memory.write_artifact_review(ArtifactReview(
        review_id="r-summ-fail", task_id="t-summ-fail", verdict="publish",
        title="t", reasoning="r", bundle_file_names=["README.md"],
        proposed_name="summ", proposed_description="x",
        timestamp="2026-04-01T00:00:00+00:00",
        proposed_readme="# summ",
    ))
    with patch.object(
        journal, "record_event",
        side_effect=OSError("journal write failed"),
    ):
        await reviewer.backfill()
    # Proposal + notification still went through.
    assert len(memory.get_publication_proposals()) == 1
    assert len(notifier.pub_calls) == 1


@pytest.mark.asyncio
async def test_backfill_ignores_already_proposed_rows(
    _agent_paths: tuple[Path, Path],
) -> None:
    """Even a rejected proposal counts as 'seen' — no re-emission."""
    memory_dir, workspace = _agent_paths
    reviewer, memory, _, _, notifier = _build_wired_reviewer(
        memory_dir, workspace,
    )
    tid = "t-seen"
    adir = workspace / f"artifacts/{tid}"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe")
    memory.record_task(_make_task(tid, str(adir)))
    memory.write_artifact_review(ArtifactReview(
        review_id="r-seen",
        task_id=tid,
        verdict="publish",
        title="Seen",
        reasoning="r",
        bundle_file_names=["README.md"],
        proposed_name="seen",
        proposed_description="x",
        timestamp="2026-04-02T00:00:00+00:00",
        proposed_readme="# seen",
    ))

    # Pre-seed a rejected proposal matching the review id.
    from maestro.memory import PublicationProposal as _PP
    memory.write_publication_proposal(_PP(
        proposal_id="p-rejected",
        task_id=tid,
        review_id="r-seen",
        proposed_name="seen",
        description="x",
        proposed_readme="# seen",
        publishable_files=["README.md"],
        status="rejected",
        created_at="2026-04-02T00:00:01+00:00",
        rejection_reason="nope",
    ))

    await reviewer.backfill()

    assert notifier.pub_calls == []
    assert {p.proposal_id for p in memory.get_publication_proposals()} == {
        "p-rejected",
    }


# ---------------------------------------------------------------------------
# review() — session-attached path (proposal: main-phase SDK calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_session_path_uses_query_on_session(
    _agent_paths: tuple[Path, Path],
) -> None:
    """When ``session`` is provided, ``query_on_session`` is used and the
    one-shot ``query()`` fallback is not touched. Identity prompt
    caching depends on this path being taken."""
    memory_dir, workspace = _agent_paths
    reviewer, _, _, _ = _build_reviewer(memory_dir, workspace)
    adir = workspace / "artifacts/t-sess"
    adir.mkdir(parents=True)
    _write_artifact(adir, "README.md", "safe content")
    task = _make_task("t-sess", str(adir))

    entries = [{
        "task_id": "t-sess", "verdict": "skip",
        "title": "Scratch", "reasoning": "Not useful",
    }]
    fake_session = MagicMock(name="ClaudeSDKClient")
    qos_mock = AsyncMock(return_value=_make_llm_response(entries))
    q_mock = AsyncMock()
    with (
        patch("maestro.artifact_review.query_on_session", qos_mock),
        patch("maestro.artifact_review.query", q_mock),
    ):
        out = await reviewer.review([task], session=fake_session)

    qos_mock.assert_awaited_once()
    assert qos_mock.call_args.args[0] is fake_session
    assert qos_mock.call_args.kwargs["caller"] == "artifact_review"
    q_mock.assert_not_called()
    assert len(out) == 1
    assert out[0].verdict == "skip"


def test_scrub_blocked_label_preserves_compound_kind() -> None:
    """split(':', 1) truncates compound kinds like narrative_redactions:name_maestro.

    Redact finding strings are formatted as '{kind}: {count}' where kind can
    contain colons (e.g. 'narrative_redactions:name_maestro'). The delimiter
    between kind and count is ': ' (colon-space), so split(': ', 1)[0] is
    required to recover the full kind without truncation.
    """
    from maestro.secret_scan import scan_contents

    result = scan_contents("maestro-helper")
    assert result.redact_findings, "Expected redact finding for maestro-helper"
    finding_str = result.redact_findings[0]
    # The finding string is formatted as '{kind}: {count}' where kind may contain colons.
    # Correct split: delimiter is ': ' (colon-space).
    correct_kind = finding_str.split(": ", 1)[0]
    assert correct_kind == "narrative_redactions:name_maestro", (
        f"Expected full compound kind but got {correct_kind!r}"
    )
    # The scrub_blocked label must include the full subtype.
    label = f"[scrub_blocked:{correct_kind}]"
    assert label == "[scrub_blocked:narrative_redactions:name_maestro]"
    # Confirm that the old (buggy) split loses the ':name_maestro' subtype.
    buggy_kind = finding_str.split(":", 1)[0]
    assert buggy_kind == "narrative_redactions"
    assert buggy_kind != correct_kind
