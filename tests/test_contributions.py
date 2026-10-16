"""Tests for ContributionTracker — VCS calls are mocked via protocol."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from maestro.contributions import ContributionTracker
from maestro.vcs import CommitInfo

FAKE_DIR = Path("/fake/contrib")
HASH_A = "aaa1111111111111111111111111111111111111"
HASH_B = "bbb2222222222222222222222222222222222222"
HASH_C = "ccc3333333333333333333333333333333333333"


def _tracker(vcs: MagicMock | None = None) -> ContributionTracker:
    return ContributionTracker(
        workspace=FAKE_DIR, memory_dir=FAKE_DIR, vcs=vcs,
    )


# ------------------------------------------------------------------
# get_current_head
# ------------------------------------------------------------------


def test_get_current_head(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_A
    t = _tracker(vcs)

    result = t.get_current_head()

    assert result == HASH_A
    vcs.get_head_hash.assert_called_once()


def test_get_current_head_failure(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_head_hash.return_value = None
    t = _tracker(vcs)

    result = t.get_current_head()

    assert result is None


# ------------------------------------------------------------------
# load / save last known commit
# ------------------------------------------------------------------


def test_load_last_known_commit_no_file(mem_fs: dict[str, str]) -> None:
    t = _tracker()
    assert t.load_last_known_commit() is None


def test_load_save_last_known_commit(mem_fs: dict[str, str]) -> None:
    t = _tracker()
    t.save_last_known_commit(HASH_A)
    assert t.load_last_known_commit() == HASH_A


def test_load_last_known_commit_oserror(mem_fs: dict[str, str]) -> None:
    """Non-FileNotFoundError OSError returns None instead of crashing."""
    t = _tracker()
    _patched_read = Path.read_text

    def _raise_for_commit_path(
        self: Path, encoding: str | None = None, errors: str | None = None,
    ) -> str:
        if str(self) == str(t._commit_path):
            raise PermissionError("denied")
        return _patched_read(self, encoding=encoding, errors=errors)

    with patch.object(Path, "read_text", _raise_for_commit_path):
        assert t.load_last_known_commit() is None


def test_save_last_known_commit_oserror(mem_fs: dict[str, str]) -> None:
    """Write failure is logged, not raised."""
    t = _tracker()

    with patch("maestro.contributions.atomic_write", side_effect=PermissionError("denied")):
        t.save_last_known_commit(HASH_A)  # should not raise
    # File was not written, so load returns None.
    assert t.load_last_known_commit() is None


def test_load_last_known_commit_with_only_whitespace(mem_fs: dict[str, str]) -> None:
    """A file containing only whitespace should return None."""
    t = _tracker()
    mem_fs[str(t._commit_path)] = "   \n  "
    result = t.load_last_known_commit()
    assert result is None


# ------------------------------------------------------------------
# own commits
# ------------------------------------------------------------------


def test_record_own_commit(mem_fs: dict[str, str]) -> None:
    t = _tracker()
    t.record_own_commit(HASH_A)
    t.record_own_commit(HASH_B)
    own = t.load_own_commits()
    assert own == {HASH_A, HASH_B}


def test_load_own_commits_no_file(mem_fs: dict[str, str]) -> None:
    t = _tracker()
    assert t.load_own_commits() == set()


def test_own_commits_cache_invalidation(mem_fs: dict[str, str]) -> None:
    """Recording a commit updates the in-memory cache without re-reading disk."""
    t = _tracker()
    assert t.load_own_commits() == set()
    t.record_own_commit(HASH_A)
    # Should reflect the new hash via cache, not a fresh disk read.
    assert HASH_A in t.load_own_commits()


def test_load_own_commits_oserror(mem_fs: dict[str, str]) -> None:
    """Non-FileNotFoundError OSError returns empty set instead of crashing."""
    t = _tracker()
    _patched_read = Path.read_text

    def _raise_for_own_commits(
        self: Path, encoding: str | None = None, errors: str | None = None,
    ) -> str:
        if str(self) == str(t._own_commits_path):
            raise PermissionError("denied")
        return _patched_read(self, encoding=encoding, errors=errors)

    with patch.object(Path, "read_text", _raise_for_own_commits):
        assert t.load_own_commits() == set()


def test_record_own_commit_oserror(mem_fs: dict[str, str]) -> None:
    """Write failure is logged, cache is not updated."""
    t = _tracker()
    # Prime the cache.
    assert t.load_own_commits() == set()
    with patch("builtins.open", side_effect=PermissionError("denied")):
        t.record_own_commit(HASH_A)  # should not raise
    # Cache should NOT contain the hash since write failed.
    assert HASH_A not in t.load_own_commits()


def test_is_valid_hex_hash_accepts_sha256(mem_fs: dict[str, str]) -> None:
    """SHA-256 hashes (64 hex chars) must be accepted by _is_valid_hex_hash."""
    sha256_hash = "a3f1" * 16  # 64-char valid hex string
    assert ContributionTracker._is_valid_hex_hash(sha256_hash), (
        f"SHA-256 hash ({len(sha256_hash)} chars) was incorrectly rejected; "
        "pattern must allow up to 64 hex characters."
    )
    # Still rejects hashes longer than 64 chars
    assert not ContributionTracker._is_valid_hex_hash("a" * 65), (
        "Pattern should reject hashes longer than 64 characters."
    )
    # Still accepts standard SHA-1 full hash (40 chars)
    assert ContributionTracker._is_valid_hex_hash("a" * 40), (
        "Pattern should still accept SHA-1 full hashes (40 hex chars)."
    )
    # Still rejects hashes shorter than 4 chars
    assert not ContributionTracker._is_valid_hex_hash("abc"), (
        "Pattern should reject hashes shorter than 4 characters."
    )


def test_record_own_commit_accepts_sha256_hash(mem_fs: dict[str, str]) -> None:
    """record_own_commit must store a 64-char SHA-256 hash without warning."""
    sha256_hash = "a3f1" * 16  # 64-char valid hex string
    t = _tracker()
    with patch("maestro.contributions.logger") as mock_logger:
        t.record_own_commit(sha256_hash)
        mock_logger.warning.assert_not_called()
    assert sha256_hash in t.load_own_commits()


def test_record_own_commit_rejects_embedded_newline(mem_fs: dict[str, str]) -> None:
    """When a hash contains an embedded newline, the code should warn and discard it."""
    t = _tracker()
    malformed_hash = "abc\ndef"

    with patch("maestro.contributions.logger") as mock_logger:
        t.record_own_commit(malformed_hash)
        assert mock_logger.warning.called, (
            "Expected a warning for malformed hash with embedded newline, but none was logged. "
            "The code silently strips the newline instead of rejecting the input."
        )

    own_commits_file = str(FAKE_DIR / "own_commits.txt")
    stored = mem_fs.get(own_commits_file, "")
    assert "abcdef" not in stored, (
        f"Malformed hash was silently stored as 'abcdef' in {own_commits_file!r}. "
        "The hash should have been rejected entirely."
    )


def test_record_own_commit_empty_hash_rejected(mem_fs: dict[str, str]) -> None:
    """record_own_commit with empty string should not pollute the cache."""
    t = _tracker()
    t.load_own_commits()  # initialize cache

    t.record_own_commit("")

    cached = t._own_commits_cache
    assert cached is not None
    assert "" not in cached, (
        "record_own_commit should reject empty commit hashes — "
        "they create cache/file inconsistency"
    )


def test_record_own_commit_whitespace_trimmed(mem_fs: dict[str, str]) -> None:
    """Hashes with leading/trailing whitespace are stored consistently after strip."""
    t = _tracker()
    raw_hash = "  abc123  "
    t.record_own_commit(raw_hash)

    # Load from disk (cache starts as None, so this reads the file).
    loaded = t.load_own_commits()

    stripped = raw_hash.strip()
    assert stripped in loaded, (
        f"Expected stripped hash {stripped!r} in loaded set {loaded!r} — "
        "record_own_commit should normalise the hash before storing"
    )
    assert len(loaded) == 1, (
        f"Expected exactly 1 entry after recording one commit, got {loaded!r}"
    )


def test_record_own_commit_newline_creates_no_phantom_entries(
    mem_fs: dict[str, str],
) -> None:
    """Malformed hash with embedded newline is rejected entirely — no phantom entries."""
    d = Path("/fake/contrib2")
    tracker = ContributionTracker(d, d)

    malformed_hash = "abc123\ndef456"
    tracker.record_own_commit(malformed_hash)

    # Invalidate cache and reload from disk.
    tracker._own_commits_cache = None
    loaded = tracker.load_own_commits()

    assert loaded == set(), (
        f"Malformed hash should have been rejected, but got {loaded}"
    )


def test_record_own_commit_rejects_non_hex(mem_fs: dict[str, str]) -> None:
    """record_own_commit should reject non-hex strings."""
    t = _tracker()
    t.record_own_commit("not-a-real-hash!")
    own = t.load_own_commits()
    assert "not-a-real-hash!" not in own


def test_record_own_commit_rejects_spaces_only(mem_fs: dict[str, str]) -> None:
    """Whitespace-only hashes should be rejected."""
    t = _tracker()
    t.record_own_commit("   ")
    own = t.load_own_commits()
    assert len(own) == 0


def test_record_own_commit_rejects_tab_chars(mem_fs: dict[str, str]) -> None:
    """Commit hash containing tab should be rejected."""
    t = _tracker()
    t.record_own_commit("abc\t123")
    own = t.load_own_commits()
    assert "abc\t123" not in own


def test_load_own_commits_returns_copy(mem_fs: dict[str, str]) -> None:
    """Mutating the returned set should NOT affect the tracker's state."""
    t = _tracker(vcs=MagicMock())
    own = t.load_own_commits()
    own.add("fake_hash")

    # If the cache is returned by reference, this mutation sticks
    reloaded = t.load_own_commits()
    assert "fake_hash" not in reloaded, (
        "load_own_commits returns mutable cache reference. "
        "Caller mutations corrupt tracker state."
    )


# ------------------------------------------------------------------
# get_new_commits
# ------------------------------------------------------------------


def test_get_new_commits(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_commits_between.return_value = [
        CommitInfo(hash=HASH_B, subject="Add feature", author="Thomas"),
        CommitInfo(hash=HASH_C, subject="Fix bug", author="Thomas"),
    ]
    t = _tracker(vcs)

    commits = t.get_new_commits(HASH_A, HASH_C)

    assert len(commits) == 2
    assert commits[0].hash == HASH_B
    assert commits[0].subject == "Add feature"
    assert commits[0].author == "Thomas"
    assert commits[1].hash == HASH_C
    vcs.get_commits_between.assert_called_once_with(HASH_A, HASH_C)


def test_get_new_commits_empty(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_commits_between.return_value = []
    t = _tracker(vcs)

    commits = t.get_new_commits(HASH_A, HASH_A)

    assert commits == []


def test_get_new_commits_failure(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_commits_between.return_value = []
    t = _tracker(vcs)

    commits = t.get_new_commits(HASH_A, HASH_B)

    assert commits == []


# ------------------------------------------------------------------
# get_diff_stat
# ------------------------------------------------------------------


def test_get_diff_stat(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_diff_stat.return_value = " maestro/agent.py | 5 ++---\n 1 file changed"
    t = _tracker(vcs)

    result = t.get_diff_stat(HASH_A, HASH_B)

    assert "agent.py" in result
    vcs.get_diff_stat.assert_called_once_with(HASH_A, HASH_B)


def test_get_diff_stat_failure(mem_fs: dict[str, str]) -> None:
    vcs = MagicMock()
    vcs.get_diff_stat.return_value = ""
    t = _tracker(vcs)

    result = t.get_diff_stat(HASH_A, HASH_B)

    assert result == ""


# ------------------------------------------------------------------
# check_for_contributions — integration
# ------------------------------------------------------------------


def test_check_for_contributions_first_run(mem_fs: dict[str, str]) -> None:
    """First run: save HEAD, no journal entry."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_A
    t = _tracker(vcs)
    journal = MagicMock()

    t.check_for_contributions(journal)

    assert t.load_last_known_commit() == HASH_A
    journal.record_event.assert_not_called()


def test_check_for_contributions_no_changes(mem_fs: dict[str, str]) -> None:
    """Same hash: no journal entry."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_A
    t = _tracker(vcs)
    t.save_last_known_commit(HASH_A)
    journal = MagicMock()

    t.check_for_contributions(journal)

    journal.record_event.assert_not_called()


def test_check_for_contributions_new_commits(mem_fs: dict[str, str]) -> None:
    """External commits trigger a contribution journal entry."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_B
    vcs.get_commits_between.return_value = [
        CommitInfo(hash=HASH_B, subject="Improve docs", author="Thomas"),
    ]
    vcs.get_diff_stat.return_value = "1 file changed"
    t = _tracker(vcs)
    t.save_last_known_commit(HASH_A)
    journal = MagicMock()

    t.check_for_contributions(journal)

    journal.record_event.assert_called_once()
    kw = journal.record_event.call_args[1]
    assert kw["entry_type"] == "contribution"
    assert kw["triggered_by"] == "contribution_tracking"
    assert "Thomas" in kw["content"]
    assert "Improve docs" in kw["content"]
    assert t.load_last_known_commit() == HASH_B


def test_check_for_contributions_filters_own_commits(mem_fs: dict[str, str]) -> None:
    """All commits are Maestro's own — no journal entry, no diff stat call."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_B
    vcs.get_commits_between.return_value = [
        CommitInfo(hash=HASH_B, subject="maestro: self-improve", author="Maestro"),
    ]
    t = _tracker(vcs)
    t.save_last_known_commit(HASH_A)
    t.record_own_commit(HASH_B)
    journal = MagicMock()

    t.check_for_contributions(journal)

    journal.record_event.assert_not_called()
    # diff stat should NOT be called when all commits are filtered out.
    vcs.get_diff_stat.assert_not_called()
    assert t.load_last_known_commit() == HASH_B


def test_check_for_contributions_mixed_commits(mem_fs: dict[str, str]) -> None:
    """Mix of own + external commits: only external appear in journal."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_C
    vcs.get_commits_between.return_value = [
        CommitInfo(hash=HASH_B, subject="maestro: evolve", author="Maestro"),
        CommitInfo(hash=HASH_C, subject="Add tests", author="Thomas"),
    ]
    vcs.get_diff_stat.return_value = "2 files changed"
    t = _tracker(vcs)
    t.save_last_known_commit(HASH_A)
    t.record_own_commit(HASH_B)
    journal = MagicMock()

    t.check_for_contributions(journal)

    journal.record_event.assert_called_once()
    content = journal.record_event.call_args[1]["content"]
    assert "1 new commit" in content
    assert "Thomas" in content
    assert "Add tests" in content
    # Maestro's commit should NOT appear.
    assert "maestro: evolve" not in content


def test_check_for_contributions_mixed_commits_diff_stat_excludes_own(
    mem_fs: dict[str, str],
) -> None:
    """When commits are mixed own + external, diff_stat must not include
    Maestro's own changes — that would overstate collaborator contributions."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_C
    vcs.get_commits_between.return_value = [
        CommitInfo(hash=HASH_B, subject="maestro: evolve", author="Maestro"),
        CommitInfo(hash=HASH_C, subject="Add tests", author="Thomas"),
    ]
    vcs.get_diff_stat.return_value = "2 files changed"
    t = _tracker(vcs)
    t.save_last_known_commit(HASH_A)
    t.record_own_commit(HASH_B)
    journal = MagicMock()

    t.check_for_contributions(journal)

    # diff_stat must NOT be called with full range (HASH_A, HASH_C) when
    # Maestro's own commits are mixed in — that overstates external changes.
    vcs.get_diff_stat.assert_not_called()


def test_check_for_contributions_subprocess_failure(mem_fs: dict[str, str]) -> None:
    """VCS failure: no crash, no journal entry, hash not updated."""
    vcs = MagicMock()
    vcs.get_head_hash.return_value = None
    t = _tracker(vcs)
    t.save_last_known_commit(HASH_A)
    journal = MagicMock()

    t.check_for_contributions(journal)

    journal.record_event.assert_not_called()
    # Hash should remain unchanged.
    assert t.load_last_known_commit() == HASH_A


def test_check_for_contributions_journal_failure_preserves_last_commit(
    mem_fs: dict[str, str],
) -> None:
    """When journal.record_event raises, last_known_commit must NOT be updated.

    If the commit is updated before journaling, a journal failure permanently
    loses contribution data — the next run won't see those commits again.
    """
    vcs = MagicMock()
    vcs.get_head_hash.return_value = HASH_B
    vcs.get_commits_between.return_value = [
        CommitInfo(hash=HASH_B, subject="Thomas fix", author="Thomas"),
    ]
    vcs.get_diff_stat.return_value = "1 file changed"
    t = _tracker(vcs)
    t.save_last_known_commit(HASH_A)

    journal = MagicMock()
    journal.record_event.side_effect = OSError("disk full")

    t.check_for_contributions(journal)

    # The last known commit should NOT have been updated since journaling failed.
    # This ensures the contributions will be retried on the next run.
    assert t.load_last_known_commit() == HASH_A, (
        "last_known_commit was updated despite journal failure — contributions lost"
    )


def test_check_for_contributions_vcs_error_does_not_advance_baseline(
    mem_fs: dict[str, str],
) -> None:
    """If get_new_commits returns [] due to VCS error, baseline must NOT advance."""
    mem_fs[str(FAKE_DIR / "last_known_commit.txt")] = "aaa111"
    tracker = ContributionTracker(FAKE_DIR, FAKE_DIR)
    tracker._vcs = MagicMock()
    tracker._vcs.get_head_hash.return_value = "bbb222"
    # Simulate VCS error — returns empty list (best-effort method)
    tracker._vcs.get_commits_between.return_value = []

    journal = MagicMock()
    tracker.check_for_contributions(journal)

    saved = mem_fs.get(str(FAKE_DIR / "last_known_commit.txt"))
    assert saved == "aaa111", (
        "Baseline was advanced despite empty commit list — real commits may be lost"
    )


def test_check_for_contributions_survives_diff_stat_failure(
    mem_fs: dict[str, str],
) -> None:
    """Contribution journaling should succeed even when diff_stat fails."""
    d = Path("/fake/contrib9")
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "newhead1234567890abcdef1234567890abcdef12"
    vcs.get_commits_between.return_value = [
        CommitInfo(hash="abc1234567890abcdef1234567890abcdef123456", subject="Fix thing", author="Thomas"),
    ]
    vcs.get_diff_stat.side_effect = RuntimeError("diff stat failed")

    tracker = ContributionTracker(d, d, vcs=vcs)
    mem_fs[str(d / "last_known_commit.txt")] = "oldhead4567890abcdef1234567890abcdef123456"

    journal = MagicMock()
    tracker.check_for_contributions(journal)

    # Journal should have been called despite diff_stat failure
    journal.record_event.assert_called_once()
    call_kwargs = journal.record_event.call_args
    content = call_kwargs.kwargs.get("content", call_kwargs.args[0] if call_kwargs.args else "")
    assert "Thomas" in content


# ------------------------------------------------------------------
# compact_own_commits / _maybe_compact_own_commits
# ------------------------------------------------------------------

BASELINE = "deadbeef" + "0" * 32  # 40-char hex baseline for last_known_commit


def test_compact_own_commits_no_baseline(mem_fs: dict[str, str]) -> None:
    """No last_known_commit → returns 0, file untouched, vcs not consulted."""
    vcs = MagicMock()
    t = _tracker(vcs)
    mem_fs[str(t._own_commits_path)] = HASH_A + "\n"

    result = t.compact_own_commits()

    assert result == 0
    vcs.is_ancestor.assert_not_called()
    assert mem_fs[str(t._own_commits_path)] == HASH_A + "\n"


def test_compact_own_commits_drops_ancestors(mem_fs: dict[str, str]) -> None:
    """Drop entries that are ancestors of last_known_commit; rewrite atomically; invalidate cache."""
    vcs = MagicMock()
    # HASH_A and HASH_B are ancestors; HASH_C is not.
    vcs.is_ancestor.side_effect = lambda h, _base: h in {HASH_A, HASH_B}
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = "\n".join([HASH_A, HASH_B, HASH_C]) + "\n"
    # Prime the cache so we can assert it gets invalidated by compaction.
    t.load_own_commits()
    assert t._own_commits_cache is not None

    result = t.compact_own_commits()

    assert result == 2
    # Cache must be invalidated so the next load_own_commits re-reads disk.
    assert t._own_commits_cache is None
    on_disk = mem_fs[str(t._own_commits_path)]
    assert HASH_C in on_disk
    assert HASH_A not in on_disk
    assert HASH_B not in on_disk


def test_compact_own_commits_keeps_all_when_none_ancestral(mem_fs: dict[str, str]) -> None:
    """No ancestors found → no rewrite, return 0 (avoids spurious atomic-write churn)."""
    vcs = MagicMock()
    vcs.is_ancestor.return_value = False
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    pre = "\n".join([HASH_A, HASH_B]) + "\n"
    mem_fs[str(t._own_commits_path)] = pre

    with patch("maestro.contributions.atomic_write") as mock_write:
        result = t.compact_own_commits()

    assert result == 0
    mock_write.assert_not_called()
    assert mem_fs[str(t._own_commits_path)] == pre


def test_compact_own_commits_idempotent_when_nothing_to_drop(mem_fs: dict[str, str]) -> None:
    """Repeated compact_own_commits calls when nothing has changed are no-ops."""
    vcs = MagicMock()
    vcs.is_ancestor.return_value = False
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_A + "\n"

    assert t.compact_own_commits() == 0
    with patch("maestro.contributions.atomic_write") as mock_write:
        assert t.compact_own_commits() == 0
    mock_write.assert_not_called()


def test_compact_own_commits_oserror_on_read(mem_fs: dict[str, str]) -> None:
    """Read failure returns 0, no exception, vcs never consulted."""
    vcs = MagicMock()
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_A + "\n"

    _real_read = Path.read_text

    def _raise_for_own_commits(
        self: Path, encoding: str | None = None, errors: str | None = None,
    ) -> str:
        if str(self) == str(t._own_commits_path):
            raise PermissionError("denied")
        return _real_read(self, encoding=encoding, errors=errors)

    with patch.object(Path, "read_text", _raise_for_own_commits):
        result = t.compact_own_commits()

    assert result == 0
    vcs.is_ancestor.assert_not_called()


def test_compact_own_commits_oserror_on_write(mem_fs: dict[str, str]) -> None:
    """atomic_write failure returns 0, no exception, cache not reset."""
    vcs = MagicMock()
    vcs.is_ancestor.return_value = True
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_A + "\n"
    # Prime the cache.
    t.load_own_commits()

    with patch(
        "maestro.contributions.atomic_write",
        side_effect=PermissionError("denied"),
    ):
        result = t.compact_own_commits()

    assert result == 0
    # Cache must NOT be reset since the rewrite failed; load_own_commits
    # must continue to reflect what's still on disk.
    assert t._own_commits_cache == {HASH_A}


def test_compact_own_commits_dedupes_duplicate_hashes(mem_fs: dict[str, str]) -> None:
    """Duplicate lines in the file are deduped before counting drops; rewrite is empty when all drop."""
    vcs = MagicMock()
    vcs.is_ancestor.return_value = True
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    # Same hash twice — defensive dedupe handles accidental dupes from
    # crashes mid-write.
    mem_fs[str(t._own_commits_path)] = HASH_A + "\n" + HASH_A + "\n"

    result = t.compact_own_commits()

    # After dedupe: {HASH_A}; after is_ancestor=True: kept = set(); dropped = 1.
    assert result == 1
    assert mem_fs[str(t._own_commits_path)] == ""


def test_compact_own_commits_treats_non_ancestor_as_kept(mem_fs: dict[str, str]) -> None:
    """A hash that is not an ancestor (e.g. force-pushed away) is kept."""
    vcs = MagicMock()
    # Simulate a commit no longer in tree: is_ancestor returns False.
    vcs.is_ancestor.return_value = False
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_A + "\n"

    result = t.compact_own_commits()

    assert result == 0
    assert HASH_A in mem_fs[str(t._own_commits_path)]


def test_record_own_commit_skips_compaction_below_threshold(
    mem_fs: dict[str, str],
) -> None:
    """A single recorded hash is well below the threshold — no vcs call."""
    vcs = MagicMock()
    t = _tracker(vcs)

    t.record_own_commit(HASH_A)

    vcs.is_ancestor.assert_not_called()


def test_record_own_commit_triggers_compaction_above_threshold(
    mem_fs: dict[str, str],
) -> None:
    """When the file size crosses the threshold, record_own_commit drives compaction."""
    vcs = MagicMock()
    vcs.is_ancestor.return_value = True
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_B + "\n"

    big_size = ContributionTracker._OWN_COMMITS_COMPACT_THRESHOLD * 41 + 1

    def fake_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        return MagicMock(st_size=big_size)

    with patch.object(Path, "stat", fake_stat):
        t.record_own_commit(HASH_A)

    vcs.is_ancestor.assert_called()
    # Both hashes were ancestors → file rewritten with empty content.
    assert mem_fs.get(str(t._own_commits_path), "") == ""


def test_record_own_commit_compaction_cooldown_suppresses_repeat_sweep(
    mem_fs: dict[str, str],
) -> None:
    """When compaction finds nothing droppable, the next record skips the sweep.

    Regression for #1771: without the cooldown, every record_own_commit
    above the size threshold re-runs the O(N) is_ancestor sweep when no
    entries are ancestors of last_known_commit.
    """
    vcs = MagicMock()
    vcs.is_ancestor.return_value = False
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_B + "\n"

    big_size = ContributionTracker._OWN_COMMITS_COMPACT_THRESHOLD * 41 + 1

    def fake_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        return MagicMock(st_size=big_size)

    with patch.object(Path, "stat", fake_stat):
        t.record_own_commit(HASH_A)
        calls_after_first = vcs.is_ancestor.call_count
        assert calls_after_first > 0
        assert t._compaction_attempted_at_size == big_size

        t.record_own_commit(HASH_C)

    # Second record at the same size class must hit the cooldown branch —
    # no additional is_ancestor calls.
    assert vcs.is_ancestor.call_count == calls_after_first


def test_record_own_commit_compaction_cooldown_clears_after_drop(
    mem_fs: dict[str, str],
) -> None:
    """When compaction actually drops entries, the cooldown clears.

    A subsequent record above the threshold must trigger the sweep again,
    so the fix never permanently disables compaction.
    """
    vcs = MagicMock()
    vcs.is_ancestor.return_value = True
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_B + "\n"

    big_size = ContributionTracker._OWN_COMMITS_COMPACT_THRESHOLD * 41 + 1

    def fake_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        return MagicMock(st_size=big_size)

    with patch.object(Path, "stat", fake_stat):
        t.record_own_commit(HASH_A)
        calls_after_first = vcs.is_ancestor.call_count
        assert calls_after_first > 0
        # All entries were ancestors → file rewritten; sentinel cleared.
        assert t._compaction_attempted_at_size is None

        t.record_own_commit(HASH_C)

    # Cooldown was cleared, so the second record must run the sweep again.
    assert vcs.is_ancestor.call_count > calls_after_first


def test_compact_own_commits_no_file(mem_fs: dict[str, str]) -> None:
    """File missing (not yet recorded) → returns 0, vcs not consulted."""
    vcs = MagicMock()
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    # Do not seed the own_commits path.
    assert str(t._own_commits_path) not in mem_fs

    result = t.compact_own_commits()

    assert result == 0
    vcs.is_ancestor.assert_not_called()


def test_compact_own_commits_empty_after_parse(mem_fs: dict[str, str]) -> None:
    """File exists but contains only whitespace → returns 0 without consulting vcs."""
    vcs = MagicMock()
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = "   \n\n  \n"

    result = t.compact_own_commits()

    assert result == 0
    vcs.is_ancestor.assert_not_called()


def test_maybe_compact_own_commits_swallows_stat_oserror(
    mem_fs: dict[str, str],
) -> None:
    """A failing Path.stat (e.g. file vanishing) does not raise — compaction is skipped."""
    vcs = MagicMock()
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)

    def fake_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("stat denied")

    with patch.object(Path, "stat", fake_stat):
        t._maybe_compact_own_commits()  # should not raise

    vcs.is_ancestor.assert_not_called()


# ------------------------------------------------------------------
# file_lock acquisition — read-modify-write invariant (#1772)
# ------------------------------------------------------------------


def test_compact_own_commits_acquires_file_lock(mem_fs: dict[str, str]) -> None:
    """The compactor's read+write runs inside a ``file_lock`` on own_commits.txt."""
    vcs = MagicMock()
    vcs.is_ancestor.side_effect = lambda h, _base: h == HASH_A
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = "\n".join([HASH_A, HASH_B]) + "\n"

    seen: list[Path] = []

    @contextmanager
    def _spy(path: Path) -> Iterator[None]:
        seen.append(path)
        yield

    with patch("maestro.contributions.file_lock", _spy):
        t.compact_own_commits()

    assert seen == [t._own_commits_path]


def test_record_own_commit_acquires_file_lock(mem_fs: dict[str, str]) -> None:
    """Each append goes through ``file_lock`` so concurrent writers cooperate."""
    t = _tracker()

    seen: list[Path] = []

    @contextmanager
    def _spy(path: Path) -> Iterator[None]:
        seen.append(path)
        yield

    with patch("maestro.contributions.file_lock", _spy):
        t.record_own_commit(HASH_A)
        t.record_own_commit(HASH_B)

    assert seen == [t._own_commits_path, t._own_commits_path]


def test_compact_own_commits_oserror_on_lock(mem_fs: dict[str, str]) -> None:
    """file_lock acquisition failure returns 0 without raising or rewriting."""
    vcs = MagicMock()
    t = _tracker(vcs)
    t.save_last_known_commit(BASELINE)
    mem_fs[str(t._own_commits_path)] = HASH_A + "\n"

    @contextmanager
    def _boom(path: Path) -> Iterator[None]:
        raise PermissionError("lock denied")
        yield  # pragma: no cover — generator contract

    with patch("maestro.contributions.file_lock", _boom):
        result = t.compact_own_commits()

    assert result == 0
    vcs.is_ancestor.assert_not_called()
    assert mem_fs[str(t._own_commits_path)] == HASH_A + "\n"
