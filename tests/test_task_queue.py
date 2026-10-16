"""Tests for ``maestro/task_queue.py``."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.loop.task_generation import rebase_curated_queue
from maestro.task_queue import (
    QueuedTask,
    TaskQueue,
    _clean_acceptance_criteria,
    _clean_workspace_item_ids,
    _min_issue_number,
    normalize_task,
    sort_queue_oldest_bugs_first,
)


@pytest.fixture
def queue_path(mem_fs: dict[str, str]) -> Path:
    return Path("/fake/agent/task_queue.json")


def test_load_returns_empty_when_file_missing(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    assert queue.load() == []


def test_save_then_load_roundtrips(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    items = [
        QueuedTask(
            task_id="abc123def456",
            goal="probe limitation",
            linked_question="What am I?",
            rationale="anchored",
            created_at="2025-01-01T00:00:00+00:00",
            last_touched_at="2025-01-01T00:00:00+00:00",
        ),
        QueuedTask(
            task_id="ghi789jkl012",
            goal="measure something",
            linked_question=None,
            rationale=None,
            created_at="2025-01-02T00:00:00+00:00",
            last_touched_at="2025-01-02T00:00:00+00:00",
        ),
    ]
    queue.save(items)
    loaded = queue.load()
    assert [q.task_id for q in loaded] == ["abc123def456", "ghi789jkl012"]
    assert loaded[0].goal == "probe limitation"
    assert loaded[0].linked_question == "What am I?"
    assert loaded[1].linked_question is None


def test_save_is_atomic_via_atomic_write(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    queue.save([QueuedTask(task_id="abcd", goal="x")])
    # ``atomic_write`` writes to a temp file then renames; the post-rename
    # path is the only one that should appear in the in-memory fs.
    assert str(queue_path) in mem_fs


def test_head_returns_first_item_without_popping(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    queue.save([
        QueuedTask(task_id="first", goal="a"),
        QueuedTask(task_id="second", goal="b"),
    ])
    head = queue.head()
    assert head is not None
    assert head.task_id == "first"
    # Still two items on disk.
    assert len(queue.load()) == 2


def test_pop_head_removes_first_item(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    queue.save([
        QueuedTask(task_id="first", goal="a"),
        QueuedTask(task_id="second", goal="b"),
    ])
    head = queue.pop_head()
    assert head is not None
    assert head.task_id == "first"
    remaining = queue.load()
    assert [q.task_id for q in remaining] == ["second"]


def test_pop_head_on_empty_returns_none(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    assert queue.pop_head() is None


def test_load_skips_malformed_entries(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    raw = json.dumps([
        {"task_id": "good", "goal": "do the thing"},
        "not a dict",
        {"task_id": "no-goal"},
        {"task_id": "", "goal": "blank id gets fresh one"},
        {"goal": "missing id gets fresh one"},
    ])
    mem_fs[str(queue_path)] = raw
    queue = TaskQueue(queue_path)
    loaded = queue.load()
    goals = [q.goal for q in loaded]
    assert "do the thing" in goals
    assert "blank id gets fresh one" in goals
    assert "missing id gets fresh one" in goals
    assert len(loaded) == 3
    for q in loaded:
        assert isinstance(q.task_id, str) and q.task_id


def test_load_returns_empty_on_corrupt_json(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    mem_fs[str(queue_path)] = "{not json"
    queue = TaskQueue(queue_path)
    assert queue.load() == []


def test_load_returns_empty_on_non_utf8_bytes(
    mem_fs: dict[str, str], queue_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    mem_fs[str(queue_path)] = "[]"
    queue = TaskQueue(queue_path)
    err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    with caplog.at_level(logging.WARNING, logger="maestro"), \
            patch.object(Path, "read_text", side_effect=err):
        assert queue.load() == []
        assert queue.head() is None
        assert queue.pop_head() is None
        assert queue.mutate(lambda items: len(items)) == 0
    assert "Failed to load task_queue.json; treating as empty" in caplog.text


def test_load_returns_empty_on_non_list_payload(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    mem_fs[str(queue_path)] = json.dumps({"task_id": "x", "goal": "y"})
    queue = TaskQueue(queue_path)
    assert queue.load() == []


def test_load_coerces_non_string_optional_fields_to_none(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    mem_fs[str(queue_path)] = json.dumps([
        {
            "task_id": "abc",
            "goal": "g",
            "linked_question": 42,
            "rationale": ["not", "a", "string"],
        },
    ])
    queue = TaskQueue(queue_path)
    loaded = queue.load()
    assert len(loaded) == 1
    assert loaded[0].linked_question is None
    assert loaded[0].rationale is None


def test_save_persists_all_fields_in_predictable_order(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    queue.save([
        QueuedTask(
            task_id="abc",
            goal="g",
            linked_question="q",
            rationale="r",
            created_at="ts1",
            last_touched_at="ts2",
        ),
    ])
    raw = mem_fs[str(queue_path)]
    payload = json.loads(raw)
    assert payload == [
        {
            "task_id": "abc",
            "goal": "g",
            "linked_question": "q",
            "rationale": "r",
            "created_at": "ts1",
            "last_touched_at": "ts2",
            "acceptance_criteria": None,
            "workspace_moment_id": None,
            "workspace_item_ids": None,
        },
    ]


def test_load_preserves_acceptance_criteria(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """Round-trip the new ``acceptance_criteria`` field through save/load
    so a queued task can carry binding criteria to ``_run_task_phase``."""
    queue = TaskQueue(queue_path)
    queue.save([
        QueuedTask(
            task_id="abc",
            goal="g",
            acceptance_criteria=["Feature X works", "Tests cover it"],
        ),
    ])
    loaded = queue.load()
    assert len(loaded) == 1
    assert loaded[0].acceptance_criteria == ["Feature X works", "Tests cover it"]


def test_load_normalizes_malformed_acceptance_criteria(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """Non-list / blank / non-string criteria entries are scrubbed; an
    all-blank result collapses to ``None`` so downstream ``if criteria:``
    truthiness checks treat malformed payloads as missing."""
    mem_fs[str(queue_path)] = json.dumps([
        {"task_id": "a", "goal": "g", "acceptance_criteria": "not a list"},
        {"task_id": "b", "goal": "g", "acceptance_criteria": ["", "  ", 42]},
        {
            "task_id": "c", "goal": "g",
            "acceptance_criteria": [" ok ", "", "second"],
        },
    ])
    queue = TaskQueue(queue_path)
    loaded = queue.load()
    by_id = {q.task_id: q for q in loaded}
    assert by_id["a"].acceptance_criteria is None
    assert by_id["b"].acceptance_criteria is None
    assert by_id["c"].acceptance_criteria == ["ok", "second"]


def test_load_preserves_workspace_citation(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """Round-trip the conscious-access citation (proposal 130): a stamped
    ``workspace_moment_id`` and ``workspace_item_ids`` list survive save/load."""
    queue = TaskQueue(queue_path)
    queue.save([
        QueuedTask(
            task_id="abc",
            goal="g",
            workspace_moment_id="cm-123",
            workspace_item_ids=["wi-1", "wi-2"],
        ),
    ])
    loaded = queue.load()
    assert len(loaded) == 1
    assert loaded[0].workspace_moment_id == "cm-123"
    assert loaded[0].workspace_item_ids == ["wi-1", "wi-2"]


def test_load_preserves_empty_vs_none_workspace_item_ids(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """``[]`` (saw the moment, cited no item) stays distinct from ``None``
    (never cited) across a JSON round-trip — the citation-provenance contract."""
    queue = TaskQueue(queue_path)
    queue.save([
        QueuedTask(
            task_id="empty",
            goal="g",
            workspace_moment_id="cm-1",
            workspace_item_ids=[],
        ),
        QueuedTask(task_id="none", goal="g"),
    ])
    loaded = queue.load()
    by_id = {q.task_id: q for q in loaded}
    assert by_id["empty"].workspace_item_ids == []
    assert by_id["empty"].workspace_moment_id == "cm-1"
    assert by_id["none"].workspace_item_ids is None
    assert by_id["none"].workspace_moment_id is None


def test_load_coerces_malformed_workspace_citation(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """A non-string ``workspace_moment_id`` collapses to ``None``; a non-list
    ``workspace_item_ids`` collapses to ``None`` while non-string members of a
    present list are dropped (the emptied list is retained as ``[]``)."""
    mem_fs[str(queue_path)] = json.dumps([
        {
            "task_id": "a", "goal": "g",
            "workspace_moment_id": 42,
            "workspace_item_ids": "not a list",
        },
        {
            "task_id": "b", "goal": "g",
            "workspace_moment_id": "cm-2",
            "workspace_item_ids": ["wi-1", 7, None, "wi-2"],
        },
        {
            "task_id": "c", "goal": "g",
            "workspace_moment_id": "cm-3",
            "workspace_item_ids": [1, 2, 3],
        },
    ])
    queue = TaskQueue(queue_path)
    loaded = queue.load()
    by_id = {q.task_id: q for q in loaded}
    assert by_id["a"].workspace_moment_id is None
    assert by_id["a"].workspace_item_ids is None
    assert by_id["b"].workspace_moment_id == "cm-2"
    assert by_id["b"].workspace_item_ids == ["wi-1", "wi-2"]
    assert by_id["c"].workspace_item_ids == []


def test_clean_workspace_item_ids_preserves_none_vs_empty() -> None:
    """The helper keeps ``None`` vs ``[]`` distinct (unlike
    ``_clean_acceptance_criteria``, which collapses ``[]`` to ``None``)."""
    assert _clean_workspace_item_ids(None) is None
    assert _clean_workspace_item_ids("nope") is None
    assert _clean_workspace_item_ids([]) == []
    assert _clean_workspace_item_ids([1, 2]) == []
    assert _clean_workspace_item_ids(["wi-1", 2, "wi-2"]) == ["wi-1", "wi-2"]
    # ``_clean_acceptance_criteria`` collapses the empty result to ``None`` —
    # the two helpers deliberately differ on the empty case.
    assert _clean_acceptance_criteria([]) is None


def test_normalize_task_coerces_workspace_citation() -> None:
    """The SQLite backend's clean-on-write path (``normalize_task``) scrubs the
    citation fields the same way the JSON backend's clean-on-read does."""
    cleaned = normalize_task(
        QueuedTask(
            task_id="a",
            goal="g",
            workspace_moment_id=99,  # type: ignore[arg-type]
            workspace_item_ids=["wi-1", 5, "wi-2"],  # type: ignore[list-item]
        ),
    )
    assert cleaned is not None
    assert cleaned.workspace_moment_id is None
    assert cleaned.workspace_item_ids == ["wi-1", "wi-2"]


def test_load_drops_stale_requeue_count_key(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """A pre-084 entry carrying the retired ``requeue_count`` key still loads:
    ``TaskQueue.load`` builds each task from a known-field allowlist, so the
    unknown key is dropped and the field removal needs no migration."""
    mem_fs[str(queue_path)] = json.dumps([
        {"task_id": "a", "goal": "g", "requeue_count": 3},
    ])
    queue = TaskQueue(queue_path)
    loaded = queue.load()
    assert len(loaded) == 1
    assert loaded[0].task_id == "a"
    assert not hasattr(loaded[0], "requeue_count")


def test_schema_fingerprint_includes_queued_task() -> None:
    from maestro import migration
    schema = {
        c.__name__: [f.name for f in fields(c)]
        for c in migration._TRACKED_CLASSES
    }
    assert "QueuedTask" in schema
    assert "goal" in schema["QueuedTask"]
    assert "linked_question" in schema["QueuedTask"]
    assert "workspace_moment_id" in schema["QueuedTask"]
    assert "workspace_item_ids" in schema["QueuedTask"]


# ── sort_queue_oldest_bugs_first ─────────────────────────────────────


def test_sort_oldest_bugs_first_empty_queue() -> None:
    """An empty queue stays empty."""
    assert sort_queue_oldest_bugs_first([]) == []


def test_sort_oldest_bugs_first_no_bugs() -> None:
    """Queue with no issue references preserves original order."""
    q = [
        QueuedTask(task_id="a", goal="measure something"),
        QueuedTask(task_id="b", goal="explore capabilities"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result] == ["a", "b"]


def test_sort_oldest_bugs_first_bugs_bubble_up() -> None:
    """Tasks referencing #NNN come before non-bug tasks."""
    q = [
        QueuedTask(task_id="explore", goal="explore capabilities"),
        QueuedTask(task_id="bug", goal="fix #1234 crash on startup"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result] == ["bug", "explore"]


def test_sort_oldest_bugs_first_orders_by_lowest_issue() -> None:
    """Among bug tasks, lowest issue number (oldest) comes first."""
    q = [
        QueuedTask(task_id="new_bug", goal="fix #2000 new crash"),
        QueuedTask(task_id="old_bug", goal="fix #100 old crash"),
        QueuedTask(task_id="mid_bug", goal="fix #500 mid crash"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result] == ["old_bug", "mid_bug", "new_bug"]


def test_sort_oldest_bugs_first_multi_issue_uses_min() -> None:
    """When a goal references multiple issues, the lowest one is the sort key."""
    q = [
        QueuedTask(task_id="multi", goal="fix #3000 and #500"),
        QueuedTask(task_id="single", goal="fix #600"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    # multi has min=500, single has min=600 → multi first
    assert [t.task_id for t in result] == ["multi", "single"]


def test_sort_oldest_bugs_first_preserves_non_bug_order() -> None:
    """Non-bug tasks retain their original relative order."""
    q = [
        QueuedTask(task_id="x", goal="explore"),
        QueuedTask(task_id="bug", goal="fix #1234"),
        QueuedTask(task_id="y", goal="measure"),
        QueuedTask(task_id="z", goal="reflect"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    non_bugs = [t.task_id for t in result if t.task_id != "bug"]
    assert non_bugs == ["x", "y", "z"]


def test_sort_oldest_bugs_first_stable_for_same_issue() -> None:
    """Tasks referencing the same issue keep their original order."""
    q = [
        QueuedTask(task_id="first", goal="investigate #100"),
        QueuedTask(task_id="second", goal="fix #100"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result] == ["first", "second"]


def test_sort_oldest_bugs_first_does_not_mutate_input() -> None:
    """The function returns a new list without mutating the input."""
    q = [
        QueuedTask(task_id="explore", goal="explore"),
        QueuedTask(task_id="bug", goal="fix #1234"),
    ]
    original_order = [t.task_id for t in q]
    sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in q] == original_order


# ── _min_issue_number helper ─────────────────────────────────────────


def test_min_issue_number_no_issues() -> None:
    """Goals without any #NNN reference return None."""
    assert _min_issue_number("measure something") is None
    assert _min_issue_number("") is None


def test_min_issue_number_single_issue() -> None:
    """A single issue reference returns that number."""
    assert _min_issue_number("fix #42 crash") == 42


def test_min_issue_number_multiple_issues() -> None:
    """Multiple issues returns the smallest number."""
    assert _min_issue_number("fix #500 related to #100 and #300") == 100


def test_min_issue_number_duplicate_references() -> None:
    """Duplicate references are de-duped and the minimum is returned."""
    assert _min_issue_number("fix #42 see also #42") == 42


def test_min_issue_number_hash_without_digits_ignored() -> None:
    """Hash followed by non-digits is not treated as an issue number."""
    assert _min_issue_number("fix #abc problem") is None


def test_min_issue_number_zero_issue() -> None:
    """#0 is technically matched — it is a valid regex match even if
    GitHub doesn't issue number 0. The sort still works correctly
    because 0 < all positive issue numbers."""
    assert _min_issue_number("fix #0 edge case") == 0


def test_min_issue_number_large_number() -> None:
    """Very large issue numbers are handled without overflow."""
    assert _min_issue_number("fix #999999") == 999999


# ── sort_queue_oldest_bugs_first — additional edge cases ─────────────


def test_sort_oldest_bugs_first_single_bug_item() -> None:
    """A single bug-fix task in the queue is returned as-is."""
    q = [QueuedTask(task_id="only", goal="fix #42")]
    result = sort_queue_oldest_bugs_first(q)
    assert len(result) == 1
    assert result[0].task_id == "only"


def test_sort_oldest_bugs_first_single_non_bug_item() -> None:
    """A single non-bug task in the queue is returned as-is."""
    q = [QueuedTask(task_id="only", goal="explore")]
    result = sort_queue_oldest_bugs_first(q)
    assert len(result) == 1
    assert result[0].task_id == "only"


def test_sort_oldest_bugs_first_all_bugs_no_others() -> None:
    """A queue with only bug-fix tasks sorts them by issue number."""
    q = [
        QueuedTask(task_id="c", goal="fix #300"),
        QueuedTask(task_id="a", goal="fix #100"),
        QueuedTask(task_id="b", goal="fix #200"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result] == ["a", "b", "c"]


def test_sort_oldest_bugs_first_equal_issue_number_preserves_fifo() -> None:
    """Tasks referencing the same issue number retain insertion order
    (equal-age tie-break = original position)."""
    q = [
        QueuedTask(task_id="alpha", goal="investigate #200"),
        QueuedTask(task_id="beta", goal="fix #200"),
        QueuedTask(task_id="gamma", goal="retest #200"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result] == ["alpha", "beta", "gamma"]


def test_sort_oldest_bugs_first_interleaved_bugs_and_non_bugs() -> None:
    """A realistic mixed queue: bugs move to front sorted by issue,
    non-bugs stay in their original relative order after."""
    q = [
        QueuedTask(task_id="explore", goal="explore capabilities"),
        QueuedTask(task_id="bug2000", goal="fix #2000 new crash"),
        QueuedTask(task_id="measure", goal="measure something"),
        QueuedTask(task_id="bug100", goal="fix #100 old crash"),
        QueuedTask(task_id="reflect", goal="reflect on purpose"),
        QueuedTask(task_id="bug500", goal="fix #500 mid crash"),
    ]
    result = sort_queue_oldest_bugs_first(q)
    # Bugs sorted by issue number: 100, 500, 2000
    assert [t.task_id for t in result] == [
        "bug100", "bug500", "bug2000",  # bugs first, oldest issue first
        "explore", "measure", "reflect",  # non-bugs in original order
    ]


def test_sort_oldest_bugs_first_pop_head_gets_oldest_bug() -> None:
    """After sorting and saving, pop_head returns the oldest bug-fix task."""
    q = [
        QueuedTask(task_id="explore", goal="explore"),
        QueuedTask(task_id="new_bug", goal="fix #9999"),
        QueuedTask(task_id="old_bug", goal="fix #10"),
    ]
    sorted_q = sort_queue_oldest_bugs_first(q)
    # The head of the sorted queue should be the oldest bug (lowest #).
    assert sorted_q[0].task_id == "old_bug"
    assert sorted_q[0].goal == "fix #10"


def test_sort_oldest_bugs_first_created_at_is_preserved() -> None:
    """The sort does not alter ``created_at`` or other fields — it only
    reorders tasks by issue number."""
    q = [
        QueuedTask(
            task_id="new", goal="fix #500",
            created_at="2026-05-28T00:00:00Z",
            rationale="recent",
        ),
        QueuedTask(
            task_id="old", goal="fix #100",
            created_at="2026-05-01T00:00:00Z",
            rationale="old",
        ),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert result[0].task_id == "old"
    assert result[0].created_at == "2026-05-01T00:00:00Z"
    assert result[0].rationale == "old"
    assert result[1].task_id == "new"
    assert result[1].created_at == "2026-05-28T00:00:00Z"
    assert result[1].rationale == "recent"


def test_sort_oldest_bugs_first_issue_in_middle_of_goal() -> None:
    """Issue references embedded in the middle of a goal are still detected."""
    q = [
        QueuedTask(task_id="explore", goal="explore capabilities"),
        QueuedTask(
            task_id="embedded",
            goal="Investigate the root cause of #1234 in module X",
        ),
    ]
    result = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result] == ["embedded", "explore"]


def test_sort_oldest_bugs_first_many_items_deterministic() -> None:
    """Sorting is deterministic across repeated invocations."""
    q = [
        QueuedTask(task_id="e", goal="explore"),
        QueuedTask(task_id="b3", goal="fix #300"),
        QueuedTask(task_id="m", goal="measure"),
        QueuedTask(task_id="b1", goal="fix #100"),
        QueuedTask(task_id="r", goal="reflect"),
        QueuedTask(task_id="b2", goal="fix #200"),
    ]
    result1 = sort_queue_oldest_bugs_first(q)
    result2 = sort_queue_oldest_bugs_first(q)
    assert [t.task_id for t in result1] == [t.task_id for t in result2]


def test_sort_oldest_bugs_first_roundtrip_through_save_load(
    mem_fs: dict[str, str],
) -> None:
    """The sorted order survives a save→load roundtrip through TaskQueue."""
    path = Path("/fake/agent/task_queue.json")
    queue = TaskQueue(path)
    items = [
        QueuedTask(task_id="explore", goal="explore"),
        QueuedTask(task_id="bug2", goal="fix #2000"),
        QueuedTask(task_id="bug1", goal="fix #100"),
    ]
    sorted_items = sort_queue_oldest_bugs_first(items)
    queue.save(sorted_items)
    loaded = queue.load()
    assert [t.task_id for t in loaded] == ["bug1", "bug2", "explore"]


# ---------------------------------------------------------------------------
# Shared defensive cleaning: _clean_acceptance_criteria / normalize_task
# ---------------------------------------------------------------------------


def test_clean_acceptance_criteria_none_stays_none() -> None:
    assert _clean_acceptance_criteria(None) is None


def test_clean_acceptance_criteria_non_list_collapses_to_none() -> None:
    # A stray scalar (e.g. a JSON string) is treated as a missing field.
    assert _clean_acceptance_criteria("not-a-list") is None


def test_clean_acceptance_criteria_drops_blank_and_non_string() -> None:
    assert _clean_acceptance_criteria(
        ["  keep  ", "", "   ", 7],  # type: ignore[list-item]
    ) == ["keep"]


def test_clean_acceptance_criteria_all_dropped_collapses_to_none() -> None:
    assert _clean_acceptance_criteria(["", "   "]) is None


def test_normalize_task_keeps_clean_task_unchanged() -> None:
    task = QueuedTask(
        task_id="t1",
        goal="real goal",
        linked_question="q",
        rationale="r",
        acceptance_criteria=["a"],
    )
    assert normalize_task(task) == task


def test_normalize_task_drops_blank_goal() -> None:
    assert normalize_task(QueuedTask(task_id="t1", goal="   ")) is None


def test_normalize_task_regenerates_blank_task_id() -> None:
    normalized = normalize_task(QueuedTask(task_id="", goal="real"))
    assert normalized is not None
    assert normalized.task_id  # regenerated, non-empty
    assert normalized.goal == "real"


def test_normalize_task_coerces_non_string_optionals_to_none() -> None:
    task = QueuedTask(
        task_id="t1",
        goal="real",
        linked_question=123,  # type: ignore[arg-type]
        rationale=456,  # type: ignore[arg-type]
    )
    normalized = normalize_task(task)
    assert normalized is not None
    assert normalized.linked_question is None
    assert normalized.rationale is None


# ---------------------------------------------------------------------------
# Serialised curation (#3561)
# ---------------------------------------------------------------------------


def _curation_rebase(
    snapshot: list[QueuedTask],
    survivors: list[QueuedTask],
    accepted_new: list[QueuedTask],
    queue_cap: int = 100,
) -> Callable[[list[QueuedTask]], list[QueuedTask]]:
    """Return the delta callback ``generate_task``'s save seam applies.

    Wraps the *production* helper ``rebase_curated_queue`` — the same function
    ``TaskGenerator.generate_task``'s ``_rebase`` closure calls — so these
    tests cannot drift from the real callback.
    """

    def _apply(live: list[QueuedTask]) -> list[QueuedTask]:
        rebased, _admitted = rebase_curated_queue(
            live,
            snapshot_ids={q.task_id for q in snapshot},
            survivors_by_id={q.task_id: q for q in survivors},
            incoming=accepted_new,
            queue_cap=queue_cap,
        )
        live[:] = rebased
        return rebased

    return _apply


def test_curation_mutate_does_not_resurrect_concurrently_popped_head(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """A task popped inside curation's load→save window stays popped."""
    queue = TaskQueue(queue_path)
    queue.save([
        QueuedTask(task_id="t1", goal="first"),
        QueuedTask(task_id="t2", goal="second"),
        QueuedTask(task_id="t3", goal="third"),
    ])
    # Curation loads its snapshot, then the priced SDK call runs...
    snapshot = queue.load()
    # ...and the consumer pops the head through a second backend instance
    # bound to the same path.
    popped = TaskQueue(queue_path).pop_head()
    assert popped is not None
    assert popped.task_id == "t1"

    # Curation's decision, derived from the stale snapshot: prune t3, edit t2.
    survivors = [q for q in snapshot if q.task_id != "t3"]
    for q in survivors:
        if q.task_id == "t2":
            q.goal = "second (rewritten)"
    queue.mutate(_curation_rebase(snapshot, survivors, []))

    persisted = queue.load()
    assert [q.task_id for q in persisted] == ["t2"]
    assert persisted[0].goal == "second (rewritten)"


def test_curation_mutate_preserves_concurrently_enqueued_task(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """A task appended by another writer mid-curation survives the save."""
    queue = TaskQueue(queue_path)
    queue.save([QueuedTask(task_id="t1", goal="first")])
    snapshot = queue.load()

    other = TaskQueue(queue_path)
    other.save([*other.load(), QueuedTask(task_id="t9", goal="concurrent")])

    fresh = QueuedTask(task_id="new", goal="from curation")
    queue.mutate(_curation_rebase(snapshot, list(snapshot), [fresh]))

    assert [q.task_id for q in queue.load()] == ["t1", "t9", "new"]


def test_mutate_returns_the_callback_result(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    queue.save([QueuedTask(task_id="t1", goal="first")])
    assert queue.mutate(lambda items: len(items)) == 1


def test_mutate_persists_an_in_place_edit(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    queue = TaskQueue(queue_path)
    queue.save([QueuedTask(task_id="t1", goal="first")])

    def _edit(items: list[QueuedTask]) -> None:
        items[0].goal = "edited"

    queue.mutate(_edit)
    assert queue.load()[0].goal == "edited"


def test_rebase_curated_queue_retrims_against_the_live_length(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """A concurrent enqueue shrinks the room the pre-call trim measured.

    The cap is re-applied over the *rebased* length, so new entries the
    snapshot had room for are dropped rather than pushing the live queue past
    ``task_queue_max``; existing entries are never evicted.
    """
    snapshot = [QueuedTask(task_id="t1", goal="first")]
    live = [*snapshot, QueuedTask(task_id="t9", goal="concurrent")]
    incoming = [
        QueuedTask(task_id="n1", goal="new one"),
        QueuedTask(task_id="n2", goal="new two"),
    ]

    rebased, admitted = rebase_curated_queue(
        live,
        snapshot_ids={q.task_id for q in snapshot},
        survivors_by_id={q.task_id: q for q in snapshot},
        incoming=incoming,
        queue_cap=3,
    )

    assert [q.task_id for q in admitted] == ["n1"]
    assert [q.task_id for q in rebased] == ["t1", "t9", "n1"]
    # Pure: the caller's live list is untouched.
    assert [q.task_id for q in live] == ["t1", "t9"]


def test_rebase_curated_queue_admits_nothing_when_the_live_queue_is_full(
    mem_fs: dict[str, str], queue_path: Path,
) -> None:
    """An over-cap live queue admits no new entry and evicts no existing one."""
    snapshot = [QueuedTask(task_id="t1", goal="first")]
    live = [
        *snapshot,
        QueuedTask(task_id="t8", goal="concurrent one"),
        QueuedTask(task_id="t9", goal="concurrent two"),
    ]

    rebased, admitted = rebase_curated_queue(
        live,
        snapshot_ids={q.task_id for q in snapshot},
        survivors_by_id={q.task_id: q for q in snapshot},
        incoming=[QueuedTask(task_id="n1", goal="new one")],
        queue_cap=2,
    )

    assert admitted == []
    assert [q.task_id for q in rebased] == ["t1", "t8", "t9"]
