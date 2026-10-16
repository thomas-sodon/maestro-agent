"""Tests for maestro.pr_ledger — per-PR tending ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from maestro.pr_ledger import (
    DEFAULT_MAX_ATTEMPTS_PER_HEAD,
    JsonPRLedger,
    PRLedger,
    PRLedgerBackend,
    PRLedgerEntry,
    _entry_from_raw,
    format_stuck_summary,
    load_all_pr_ledger_entries,
)
from maestro.state_store import SqlitePRLedger, StateStore

# ---------------------------------------------------------------------------
# _entry_from_raw
# ---------------------------------------------------------------------------


class TestEntryFromRaw:
    def test_valid_entry(self) -> None:
        raw = {"pr_number": 42, "branch": "maestro/evolve/foo", "stuck": False}
        entry = _entry_from_raw(raw)
        assert entry is not None
        assert entry.pr_number == 42
        assert entry.branch == "maestro/evolve/foo"

    def test_missing_pr_number_returns_none(self) -> None:
        assert _entry_from_raw({"branch": "foo"}) is None

    def test_non_int_pr_number_returns_none(self) -> None:
        assert _entry_from_raw({"pr_number": "42"}) is None

    def test_bool_pr_number_returns_none(self) -> None:
        assert _entry_from_raw({"pr_number": True}) is None

    def test_bool_int_fields_ignored(self) -> None:
        raw = {"pr_number": 1, "attempts_on_head": True, "total_attempts": True}
        entry = _entry_from_raw(raw)
        assert entry is not None
        assert entry.attempts_on_head == 0
        assert entry.total_attempts == 0

    def test_non_dict_returns_none(self) -> None:
        assert _entry_from_raw([1, 2]) is None
        assert _entry_from_raw("string") is None
        assert _entry_from_raw(None) is None

    def test_negative_int_fields_ignored(self) -> None:
        raw = {"pr_number": 1, "attempts_on_head": -1, "total_attempts": -5}
        entry = _entry_from_raw(raw)
        assert entry is not None
        assert entry.attempts_on_head == 0
        assert entry.total_attempts == 0

    def test_stuck_bool_parsed(self) -> None:
        entry = _entry_from_raw({"pr_number": 1, "stuck": True})
        assert entry is not None
        assert entry.stuck is True

    def test_non_bool_stuck_ignored(self) -> None:
        entry = _entry_from_raw({"pr_number": 1, "stuck": "yes"})
        assert entry is not None
        assert entry.stuck is False

    def test_string_fields_populated(self) -> None:
        raw = {
            "pr_number": 7,
            "observed_head": "abc123",
            "last_outcome": "noop",
            "stuck_reason": "no commit produced",
        }
        entry = _entry_from_raw(raw)
        assert entry is not None
        assert entry.observed_head == "abc123"
        assert entry.last_outcome == "noop"
        assert entry.stuck_reason == "no commit produced"

    def test_non_string_fields_ignored(self) -> None:
        raw = {"pr_number": 1, "observed_head": 99, "stuck_reason": True}
        entry = _entry_from_raw(raw)
        assert entry is not None
        assert entry.observed_head == ""
        assert entry.stuck_reason is None


# ---------------------------------------------------------------------------
# PRLedger — persistence round-trip
# ---------------------------------------------------------------------------


class TestPRLedgerPersistence:
    def test_get_returns_none_on_missing_file(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        assert ledger.get(1) is None

    def test_get_returns_none_on_corrupted_file(
        self, mem_fs: dict[str, str],
    ) -> None:
        mem_fs["/data/pr_ledger.json"] = "not-json"
        ledger = PRLedger(Path("/data"))
        assert ledger.get(1) is None

    def test_get_returns_none_on_array_root(self, mem_fs: dict[str, str]) -> None:
        mem_fs["/data/pr_ledger.json"] = "[]"
        ledger = PRLedger(Path("/data"))
        assert ledger.get(1) is None

    def test_can_attempt_true_on_fresh_ledger(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        assert ledger.can_attempt(99) is True

    def test_observe_head_creates_entry(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        entry = ledger.observe_head(1, "maestro/evolve/plan-a", "abc123")
        assert entry.pr_number == 1
        assert entry.observed_head == "abc123"
        assert entry.branch == "maestro/evolve/plan-a"

    def test_observe_head_persists(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "maestro/evolve/plan-a", "abc123")
        # New instance reads from same backing file.
        ledger2 = PRLedger(Path("/data"))
        entry = ledger2.get(1)
        assert entry is not None
        assert entry.observed_head == "abc123"

    def test_saved_payload_is_valid_json(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(42, "branch", "sha1")
        raw = json.loads(mem_fs["/data/pr_ledger.json"])
        assert "42" in raw
        assert raw["42"]["pr_number"] == 42


# ---------------------------------------------------------------------------
# PRLedger — observe_head semantics
# ---------------------------------------------------------------------------


class TestObserveHead:
    def test_same_head_does_not_reset_attempts(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.attempts_on_head == 2
        # Observe same head again — count must not reset.
        ledger.observe_head(1, "b", "sha1")
        entry2 = ledger.get(1)
        assert entry2 is not None
        assert entry2.attempts_on_head == 2

    def test_new_head_resets_attempts(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.observe_head(1, "b", "sha2")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.attempts_on_head == 0

    def test_new_head_clears_stuck(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        assert ledger.get(1) is not None
        assert ledger.get(1).stuck is True  # type: ignore[union-attr]
        ledger.observe_head(1, "b", "sha2")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.stuck is False
        assert entry.stuck_reason is None
        assert entry.escalated_at is None

    def test_none_head_does_not_reset(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.observe_head(1, "b", None)
        entry = ledger.get(1)
        assert entry is not None
        assert entry.attempts_on_head == 1

    def test_empty_head_does_not_reset(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.observe_head(1, "b", "")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.attempts_on_head == 1


# ---------------------------------------------------------------------------
# PRLedger — record_attempt / can_attempt / stuck latch
# ---------------------------------------------------------------------------


class TestRecordAttempt:
    def test_increments_attempts_on_head(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="x")
        ledger.record_attempt(1, outcome="progress", stuck_reason="y")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.attempts_on_head == 2
        assert entry.total_attempts == 2

    def test_latches_stuck_at_budget_exhaustion(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=3)
        ledger.observe_head(1, "b", "sha1")
        for _ in range(3):
            ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.stuck is True
        assert entry.stuck_reason == "no commit"
        assert entry.stuck_since is not None

    def test_not_stuck_before_budget_exhausted(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=3)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.stuck is False

    def test_progress_attempts_never_latch_stuck(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A progress lap is awaiting Tend's push, not futile — exhausting
        the budget with progress laps parks the PR without latching stuck."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="progress", stuck_reason="in-progress")
        ledger.record_attempt(1, outcome="progress", stuck_reason="in-progress")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.attempts_on_head == 2
        assert entry.stuck is False
        assert entry.stuck_reason is None
        # The budget is still consumed: re-routing parks until the head moves.
        assert ledger.can_attempt(1) is False
        # Progress attempts past the budget stay unlatched too.
        ledger.record_attempt(1, outcome="progress", stuck_reason="in-progress")
        entry_after = ledger.get(1)
        assert entry_after is not None
        assert entry_after.stuck is False

    def test_futile_attempt_at_exhaustion_still_latches(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="progress", stuck_reason="in-progress")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.stuck is True
        assert entry.stuck_reason == "no commit"

    def test_head_advance_after_progress_exhaustion_restores_budget(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="progress", stuck_reason="in-progress")
        ledger.record_attempt(1, outcome="progress", stuck_reason="in-progress")
        assert ledger.can_attempt(1) is False
        ledger.observe_head(1, "b", "sha2")
        assert ledger.can_attempt(1) is True

    def test_can_attempt_false_when_stuck(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        ledger.observe_head(1, "b", "sha1")
        assert ledger.can_attempt(1) is True
        ledger.record_attempt(1, outcome="noop", stuck_reason="blocked")
        assert ledger.can_attempt(1) is False

    def test_can_attempt_false_when_at_budget(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="x")
        ledger.record_attempt(1, outcome="noop", stuck_reason="x")
        assert ledger.can_attempt(1) is False

    def test_already_stuck_does_not_double_latch(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="first reason")
        stuck_since_first = ledger.get(1)
        assert stuck_since_first is not None
        first_since = stuck_since_first.stuck_since
        # Extra attempt after stuck — latch must not change.
        ledger.record_attempt(1, outcome="noop", stuck_reason="second reason")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.stuck_reason == "first reason"
        assert entry.stuck_since == first_since

    def test_branch_set_by_record_attempt(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.record_attempt(1, outcome="noop", stuck_reason="x", branch="my/branch")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.branch == "my/branch"

    def test_last_outcome_updated(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="x")
        ledger.record_attempt(1, outcome="progress", stuck_reason="y")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.last_outcome == "progress"

    def test_now_parameter_overrides_timestamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(1, "b", "sha1")
        fixed = datetime(2025, 1, 1, tzinfo=UTC)
        ledger.record_attempt(1, outcome="noop", stuck_reason="x", now=fixed)
        entry = ledger.get(1)
        assert entry is not None
        assert entry.last_attempt_at == fixed.isoformat()


# ---------------------------------------------------------------------------
# PRLedger — grant_fresh_attempts
# ---------------------------------------------------------------------------


class TestGrantFreshAttempts:
    def test_clears_stuck_latch_and_attempts(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A budget-exhausted stuck PR regains its full budget: a new
        conflict can only be resolved by an attempt the exhausted budget
        would otherwise block forever."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.mark_escalated(1)
        ledger.grant_fresh_attempts(1, reason="new conflict")
        entry = ledger.get(1)
        assert entry is not None
        assert entry.attempts_on_head == 0
        assert entry.stuck is False
        assert entry.stuck_reason is None
        assert entry.stuck_since is None
        assert entry.escalated_at is None
        assert ledger.can_attempt(1) is True

    def test_resets_parked_progress_budget(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A budget consumed by progress laps (parked, not stuck) also
        resets — the grant covers both exhausted classes."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="progress", stuck_reason="x")
        ledger.record_attempt(1, outcome="progress", stuck_reason="x")
        assert ledger.parked_entries() != []
        ledger.grant_fresh_attempts(1, reason="new conflict")
        assert ledger.parked_entries() == []
        assert ledger.can_attempt(1) is True

    def test_missing_entry_is_noop(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.grant_fresh_attempts(99, reason="new conflict")
        assert ledger.get(99) is None

    def test_fresh_entry_is_quiet_noop(self, mem_fs: dict[str, str]) -> None:
        """An entry with full budget and no latch has nothing to reset —
        the file is not rewritten."""
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "b", "sha1")
        before = mem_fs["/data/pr_ledger.json"]
        ledger.grant_fresh_attempts(1, reason="new conflict")
        assert mem_fs["/data/pr_ledger.json"] == before

    def test_persists_across_instances(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="no commit")
        ledger.grant_fresh_attempts(1, reason="new conflict")
        assert PRLedger(Path("/data"), max_attempts_per_head=1).can_attempt(1)


# ---------------------------------------------------------------------------
# PRLedger — mark_escalated
# ---------------------------------------------------------------------------


class TestMarkEscalated:
    def test_records_escalated_at(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        ledger.observe_head(1, "b", "sha1")
        ledger.record_attempt(1, outcome="noop", stuck_reason="reason")
        ledger.mark_escalated(1)
        entry = ledger.get(1)
        assert entry is not None
        assert entry.escalated_at is not None

    def test_no_op_on_missing_entry(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.mark_escalated(99)
        assert ledger.get(99) is None


# ---------------------------------------------------------------------------
# PRLedger — prune
# ---------------------------------------------------------------------------


class TestPrune:
    def test_removes_entries_not_in_keep(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "b1", "sha1")
        ledger.observe_head(2, "b2", "sha2")
        ledger.observe_head(3, "b3", "sha3")
        dropped = ledger.prune({1, 3})
        assert ledger.get(1) is not None
        assert ledger.get(2) is None
        assert ledger.get(3) is not None
        # Proposal 103: prune returns the dropped PR numbers so the tend pass
        # can route them into on-settlement record reconciliation.
        assert dropped == [2]

    def test_prune_returns_sorted_dropped_numbers(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"))
        # Seed out of order so the sorted-return contract is exercised.
        ledger.observe_head(30, "b30", "sha30")
        ledger.observe_head(10, "b10", "sha10")
        ledger.observe_head(20, "b20", "sha20")
        dropped = ledger.prune(set())
        assert dropped == [10, 20, 30]

    def test_prune_all_empty(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "b", "sha1")
        dropped = ledger.prune(set())
        assert ledger.get(1) is None
        assert dropped == [1]

    def test_prune_noop_when_all_in_keep(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "b", "sha1")
        file_before = mem_fs.get("/data/pr_ledger.json")
        dropped = ledger.prune({1})
        # File content must not change — no write needed.
        file_after = mem_fs.get("/data/pr_ledger.json")
        assert file_before == file_after
        # Nothing stale on the early-exit path → empty list, never None.
        assert dropped == []

    def test_prune_empty_ledger_is_noop(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        dropped = ledger.prune({1, 2, 3})
        assert "/data/pr_ledger.json" not in mem_fs
        assert dropped == []


# ---------------------------------------------------------------------------
# PRLedger — evict_settled
# ---------------------------------------------------------------------------


class TestEvictSettled:
    def test_evicts_existing_entry_removes_only_that_pr(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "b1", "sha1")
        ledger.observe_head(2, "b2", "sha2")
        ledger.observe_head(3, "b3", "sha3")
        assert ledger.evict_settled(2) is True
        # Only PR #2 is forgotten; its siblings survive.
        assert ledger.get(1) is not None
        assert ledger.get(2) is None
        assert ledger.get(3) is not None

    def test_evict_absent_entry_second_pass_is_noop(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "b1", "sha1")
        ledger.observe_head(2, "b2", "sha2")
        assert ledger.evict_settled(1) is True
        file_before = mem_fs.get("/data/pr_ledger.json")
        # A second pass over the now-absent entry returns False and writes
        # nothing — the file content is unchanged and PR #2 still survives.
        assert ledger.evict_settled(1) is False
        assert mem_fs.get("/data/pr_ledger.json") == file_before
        assert ledger.get(2) is not None

    def test_evict_empty_ledger_is_noop(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        assert ledger.evict_settled(1) is False
        assert "/data/pr_ledger.json" not in mem_fs


# ---------------------------------------------------------------------------
# PRLedger — stuck_entries
# ---------------------------------------------------------------------------


class TestStuckEntries:
    def test_no_entries_returns_empty(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        assert ledger.stuck_entries() == []

    def test_returns_only_stuck(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        ledger.observe_head(1, "b1", "sha1")
        ledger.observe_head(2, "b2", "sha2")
        ledger.record_attempt(1, outcome="noop", stuck_reason="blocked")
        stuck = ledger.stuck_entries()
        assert len(stuck) == 1
        assert stuck[0].pr_number == 1

    def test_ordered_by_pr_number(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        for pr in (5, 2, 8):
            ledger.observe_head(pr, f"b{pr}", f"sha{pr}")
            ledger.record_attempt(pr, outcome="noop", stuck_reason="x")
        stuck = ledger.stuck_entries()
        nums = [e.pr_number for e in stuck]
        assert nums == sorted(nums)


# ---------------------------------------------------------------------------
# PRLedger — parked_entries (budget exhausted by progress laps, not stuck)
# ---------------------------------------------------------------------------


class TestParkedEntries:
    def test_no_entries_returns_empty(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"))
        assert ledger.parked_entries() == []

    def test_progress_exhaustion_is_parked(self, mem_fs: dict[str, str]) -> None:
        """A PR whose budget is consumed entirely by progress laps (local
        commits that never landed) is parked but not latched stuck."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(7, "b", "sha1")
        ledger.record_attempt(7, outcome="progress", stuck_reason="in-progress")
        ledger.record_attempt(7, outcome="progress", stuck_reason="in-progress")
        parked = ledger.parked_entries()
        assert len(parked) == 1
        assert parked[0].pr_number == 7
        assert parked[0].stuck is False

    def test_under_budget_not_parked(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=3)
        ledger.observe_head(7, "b", "sha1")
        ledger.record_attempt(7, outcome="progress", stuck_reason="in-progress")
        assert ledger.parked_entries() == []

    def test_stuck_entries_are_not_parked(self, mem_fs: dict[str, str]) -> None:
        """A latched-stuck PR is reported by stuck_entries, not parked_entries —
        the two lists are disjoint so the same PR is never double-surfaced."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(7, "b", "sha1")
        ledger.record_attempt(7, outcome="noop", stuck_reason="no commit")
        ledger.record_attempt(7, outcome="noop", stuck_reason="no commit")
        assert ledger.get(7).stuck is True  # type: ignore[union-attr]
        assert ledger.parked_entries() == []
        assert len(ledger.stuck_entries()) == 1

    def test_head_advance_clears_parked(self, mem_fs: dict[str, str]) -> None:
        """Delivered head movement resets the budget, so a parked PR drops off
        the list automatically — no durable false positive survives delivery."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(7, "b", "sha1")
        ledger.record_attempt(7, outcome="progress", stuck_reason="in-progress")
        ledger.record_attempt(7, outcome="progress", stuck_reason="in-progress")
        assert len(ledger.parked_entries()) == 1
        ledger.observe_head(7, "b", "sha2")
        assert ledger.parked_entries() == []

    def test_ordered_by_pr_number(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        for pr in (5, 2, 8):
            ledger.observe_head(pr, f"b{pr}", f"sha{pr}")
            ledger.record_attempt(pr, outcome="progress", stuck_reason="x")
        nums = [e.pr_number for e in ledger.parked_entries()]
        assert nums == sorted(nums)


# ---------------------------------------------------------------------------
# known_branches (proposal 093)
# ---------------------------------------------------------------------------


class TestKnownBranches:
    def test_empty_on_missing_file(self, mem_fs: dict[str, str]) -> None:
        """A missing ledger yields an empty set and never raises."""
        assert PRLedger(Path("/data")).known_branches() == set()

    def test_empty_on_corrupted_file(self, mem_fs: dict[str, str]) -> None:
        """A corrupt ledger degrades to the empty set via tolerant _load."""
        mem_fs["/data/pr_ledger.json"] = "not-json"
        assert PRLedger(Path("/data")).known_branches() == set()

    def test_collects_recorded_branches(self, mem_fs: dict[str, str]) -> None:
        """Every non-empty recorded branch is returned, deduped across entries."""
        ledger = PRLedger(Path("/data"))
        ledger.observe_head(1, "maestro/evolve/plan-a", "abc")
        ledger.observe_head(2, "maestro/evolve/plan-b", "def")
        assert ledger.known_branches() == {
            "maestro/evolve/plan-a", "maestro/evolve/plan-b",
        }

    def test_empty_branch_filtered(self, mem_fs: dict[str, str]) -> None:
        """An entry with an empty-string branch contributes nothing."""
        mem_fs["/data/pr_ledger.json"] = json.dumps({
            "1": {"pr_number": 1, "branch": ""},
            "2": {"pr_number": 2, "branch": "maestro/evolve/plan-b"},
        })
        assert PRLedger(Path("/data")).known_branches() == {
            "maestro/evolve/plan-b",
        }


# ---------------------------------------------------------------------------
# format_stuck_summary
# ---------------------------------------------------------------------------


class TestFormatStuckSummary:
    def test_empty_when_no_stuck(self, mem_fs: dict[str, str]) -> None:
        result = format_stuck_summary(Path("/data"))
        assert result == ""

    def test_surfaces_parked_pr_on_progress_exhaustion(
        self, mem_fs: dict[str, str],
    ) -> None:
        """N consecutive progress laps at a static head exhaust the budget and
        park the PR; it must surface in the summary (not silently disappear)."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(7, "b", "sha1")
        ledger.record_attempt(7, outcome="progress", stuck_reason="in-progress")
        ledger.record_attempt(7, outcome="progress", stuck_reason="in-progress")
        assert ledger.can_attempt(7) is False
        result = format_stuck_summary(Path("/data"), max_attempts_per_head=2)
        assert "PARKED-PR" in result
        assert "#7" in result

    def test_parked_respects_configured_threshold(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The parked threshold must agree with the configured budget, not the
        default — a PR under the configured budget must not be surfaced."""
        ledger = PRLedger(Path("/data"), max_attempts_per_head=5)
        ledger.observe_head(7, "b", "sha1")
        for _ in range(4):
            ledger.record_attempt(
                7, outcome="progress", stuck_reason="in-progress",
            )
        result = format_stuck_summary(Path("/data"), max_attempts_per_head=5)
        assert result == ""

    def test_surfaces_both_stuck_and_parked(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=2)
        ledger.observe_head(10, "b10", "sha10")
        ledger.record_attempt(10, outcome="noop", stuck_reason="no commit")
        ledger.record_attempt(10, outcome="noop", stuck_reason="no commit")
        ledger.observe_head(20, "b20", "sha20")
        ledger.record_attempt(20, outcome="progress", stuck_reason="in-progress")
        ledger.record_attempt(20, outcome="progress", stuck_reason="in-progress")
        result = format_stuck_summary(Path("/data"), max_attempts_per_head=2)
        assert "STUCK-PR" in result
        assert "#10" in result
        assert "PARKED-PR" in result
        assert "#20" in result

    def test_empty_when_no_ledger_file(self, mem_fs: dict[str, str]) -> None:
        result = format_stuck_summary(Path("/data"))
        assert result == ""

    def test_surfaces_stuck_pr(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        ledger.observe_head(42, "b", "sha1")
        ledger.record_attempt(42, outcome="noop", stuck_reason="merge-blocked")
        result = format_stuck_summary(Path("/data"))
        assert "STUCK-PR" in result
        assert "#42" in result
        assert "merge-blocked" in result

    def test_lists_multiple_stuck_prs(self, mem_fs: dict[str, str]) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        for pr in (10, 20):
            ledger.observe_head(pr, f"b{pr}", f"sha{pr}")
            ledger.record_attempt(pr, outcome="noop", stuck_reason="blocked")
        result = format_stuck_summary(Path("/data"))
        assert "#10" in result
        assert "#20" in result

    def test_no_reason_falls_back_gracefully(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = PRLedger(Path("/data"), max_attempts_per_head=1)
        ledger.observe_head(5, "b", "sha1")
        ledger.record_attempt(5, outcome="noop", stuck_reason="")
        result = format_stuck_summary(Path("/data"), max_attempts_per_head=1)
        assert "STUCK-PR" in result
        assert "#5" in result
        assert "reason unrecorded" in result

    def test_reads_injected_backend_not_json(
        self, mem_fs: dict[str, str],
    ) -> None:
        """An injected *backend* is the store that is read — a stuck entry
        living only in the SQLite store surfaces even though no
        ``pr_ledger.json`` exists under ``data_dir`` (the flag-on loop path)."""
        backend = SqlitePRLedger(StateStore(":memory:"))
        ledger = PRLedger(
            Path("/data"), max_attempts_per_head=1, backend=backend,
        )
        ledger.observe_head(42, "b", "sha1")
        ledger.record_attempt(42, outcome="noop", stuck_reason="merge-blocked")
        assert "/data/pr_ledger.json" not in mem_fs

        result = format_stuck_summary(
            Path("/data"), max_attempts_per_head=1, backend=backend,
        )
        assert "STUCK-PR" in result
        assert "#42" in result


# ---------------------------------------------------------------------------
# DEFAULT_MAX_ATTEMPTS_PER_HEAD constant
# ---------------------------------------------------------------------------


def test_default_max_attempts_positive() -> None:
    assert DEFAULT_MAX_ATTEMPTS_PER_HEAD >= 1


# ---------------------------------------------------------------------------
# PRLedgerEntry dataclass defaults
# ---------------------------------------------------------------------------


def test_entry_defaults() -> None:
    entry = PRLedgerEntry(pr_number=1)
    assert entry.branch == ""
    assert entry.observed_head == ""
    assert entry.attempts_on_head == 0
    assert entry.total_attempts == 0
    assert entry.stuck is False
    assert entry.stuck_reason is None
    assert entry.stuck_since is None
    assert entry.escalated_at is None


# ---------------------------------------------------------------------------
# load_all_pr_ledger_entries — the SQLite seed source (proposal 106)
# ---------------------------------------------------------------------------


class TestLoadAllEntries:
    def test_absent_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert load_all_pr_ledger_entries(Path("/data")) == []

    def test_corrupt_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        mem_fs["/data/pr_ledger.json"] = "not-json"
        assert load_all_pr_ledger_entries(Path("/data")) == []

    def test_array_root_returns_empty(self, mem_fs: dict[str, str]) -> None:
        mem_fs["/data/pr_ledger.json"] = "[]"
        assert load_all_pr_ledger_entries(Path("/data")) == []

    def test_sorted_by_pr_number(self, mem_fs: dict[str, str]) -> None:
        """The seed order is deterministic — oldest PR number first — regardless
        of the on-disk key order."""
        mem_fs["/data/pr_ledger.json"] = json.dumps({
            "30": {"pr_number": 30, "branch": "b30"},
            "10": {"pr_number": 10, "branch": "b10"},
            "20": {"pr_number": 20, "branch": "b20"},
        })
        entries = load_all_pr_ledger_entries(Path("/data"))
        assert [e.pr_number for e in entries] == [10, 20, 30]

    def test_skips_malformed_rows(self, mem_fs: dict[str, str]) -> None:
        """Corruption tolerance mirrors the JSON adapter's read: a row missing
        ``pr_number`` or that is not a dict is skipped, not raised."""
        mem_fs["/data/pr_ledger.json"] = json.dumps({
            "1": {"pr_number": 1, "branch": "keep"},
            "bad": {"branch": "no pr_number"},
            "2": "not-a-dict",
        })
        entries = load_all_pr_ledger_entries(Path("/data"))
        assert [e.pr_number for e in entries] == [1]


# ---------------------------------------------------------------------------
# JsonPRLedger — the default backend read/mutate seam (proposal 106)
# ---------------------------------------------------------------------------


class TestJsonPRLedgerBackend:
    def test_satisfies_backend_protocol(self, mem_fs: dict[str, str]) -> None:
        assert isinstance(JsonPRLedger(Path("/data")), PRLedgerBackend)

    def test_read_empty_on_missing_file(self, mem_fs: dict[str, str]) -> None:
        assert JsonPRLedger(Path("/data")).read() == {}

    def test_read_tolerates_corruption(self, mem_fs: dict[str, str]) -> None:
        mem_fs["/data/pr_ledger.json"] = "not-json"
        assert JsonPRLedger(Path("/data")).read() == {}

    def test_mutate_persists_a_change_and_returns_result(
        self, mem_fs: dict[str, str],
    ) -> None:
        backend = JsonPRLedger(Path("/data"))

        def _add(entries: dict[int, PRLedgerEntry]) -> int:
            entries[5] = PRLedgerEntry(pr_number=5, branch="b5")
            return len(entries)

        assert backend.mutate(_add) == 1
        assert backend.read()[5].branch == "b5"

    def test_mutate_noop_writes_nothing(self, mem_fs: dict[str, str]) -> None:
        """A closure that leaves the map unchanged never creates the file — the
        "quiet no-op writes nothing" contract."""
        backend = JsonPRLedger(Path("/data"))

        def _noop(entries: dict[int, PRLedgerEntry]) -> str:
            return "untouched"

        assert backend.mutate(_noop) == "untouched"
        assert "/data/pr_ledger.json" not in mem_fs

    def test_mutate_field_change_persists(self, mem_fs: dict[str, str]) -> None:
        """Mutating a field on an existing entry (not just add/delete) is detected
        and persisted."""
        backend = JsonPRLedger(Path("/data"))
        backend.mutate(
            lambda entries: entries.__setitem__(
                5, PRLedgerEntry(pr_number=5, branch="old"),
            ),
        )

        def _rename(entries: dict[int, PRLedgerEntry]) -> None:
            entries[5].branch = "new"

        backend.mutate(_rename)
        assert backend.read()[5].branch == "new"


# ---------------------------------------------------------------------------
# PRLedger over the SQLite backend — the seam works through the public API
# ---------------------------------------------------------------------------


class TestPRLedgerOverSqliteBackend:
    def test_sqlite_backend_satisfies_protocol(self) -> None:
        assert isinstance(SqlitePRLedger(StateStore(":memory:")), PRLedgerBackend)

    def test_full_lifecycle_through_sqlite(self) -> None:
        """PRLedger drives its whole op set through an injected SqlitePRLedger over
        an in-memory StateStore — no JSON file is touched (``data_dir`` is unused
        when a backend is injected)."""
        backend = SqlitePRLedger(StateStore(":memory:"))
        ledger = PRLedger(
            Path("/unused"), max_attempts_per_head=2, backend=backend,
        )
        ledger.observe_head(7, "b7", "sha1")
        ledger.record_attempt(7, outcome="noop", stuck_reason="blocked")
        ledger.record_attempt(7, outcome="noop", stuck_reason="blocked")
        entry = ledger.get(7)
        assert entry is not None
        assert entry.stuck is True
        assert ledger.can_attempt(7) is False
        assert [e.pr_number for e in ledger.stuck_entries()] == [7]
        assert ledger.known_branches() == {"b7"}
        assert ledger.evict_settled(7) is True
        assert ledger.get(7) is None

    def test_sqlite_mutate_noop_leaves_store_empty(self) -> None:
        """A no-op eviction over an empty SQLite-backed ledger writes no row."""
        store = StateStore(":memory:")
        ledger = PRLedger(Path("/unused"), backend=SqlitePRLedger(store))
        assert ledger.evict_settled(99) is False
        assert store.load_pr_ledger() == {}
