"""Push-delivery markers, stand-down verdicts, fetch truth and the remediation
verdict gate; split out of ``tests/test_remediation_tracker.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_PENDING,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
    BranchReachability,
    FetchTruthBackend,
    FetchTruthRecord,
    JsonlFetchTruthLedger,
    JsonlObligationForkLedger,
    JsonlStandDownVerdictLedger,
    ObligationFork,
    ObligationForkRecord,
    StandDownVerdict,
    classify_committed_unpushed_push,
    coerce_verdict_to_settled_on_merge,
    fetch_truth_relap,
    find_stand_down,
    format_standdown_production,
    gate_remediation_verdict,
    get_obligation_fork,
    has_fetch_truth,
    has_live_stand_down,
    leading_same_sha_streak,
    load_all_fetch_truth_markers,
    load_all_obligation_forks,
    measure_standdown_production,
    mechanical_standdown_verdict,
    record_fetch_truth,
    record_obligation_fork_truth,
    record_stand_down,
    release_stand_downs,
    resolve_push_delivery_marker,
    version_archive_has_landing,
)
from maestro.state_store import (
    SqliteFetchTruthLedger,
    SqliteObligationForkLedger,
    StateStore,
)

# ──────────────────────────────────────────────────────────────────────
# Push-delivery marker resolver tests (proposal 098)
# ──────────────────────────────────────────────────────────────────────


class TestResolvePushDeliveryMarker:
    """The total delivery-truth → marker table replacing the two-arm shape.

    Totality is the point: the pre-098 ``if / elif`` left the never-attempted
    state unhandled, which is how the ``awaiting tend push`` external-deferral
    costume survived on a push the loop itself owns.
    """

    @pytest.mark.parametrize(
        "reachability",
        [None, BranchReachability.OWNED_AHEAD, BranchReachability.EXTERNAL],
    )
    def test_delivered_is_delivered_awaiting_merge(
        self, reachability: BranchReachability | None,
    ) -> None:
        # A successful push is delivered regardless of reachability — the
        # commit reached origin, so only a human merge is outstanding.
        assert (
            resolve_push_delivery_marker(True, reachability)
            is Marker.DELIVERED_AWAITING_MERGE
        )

    @pytest.mark.parametrize(
        "reachability",
        [None, BranchReachability.OWNED_AHEAD, BranchReachability.EXTERNAL],
    )
    def test_failed_push_is_owned_push_available(
        self, reachability: BranchReachability | None,
    ) -> None:
        # Both attempts failed: the resolution is still local, so pushing it is
        # my next act. Never the external-deferral marker my own preamble
        # forbids for an owned blocker.
        assert (
            resolve_push_delivery_marker(False, reachability)
            is Marker.OWNED_PUSH_AVAILABLE
        )

    def test_never_attempted_and_ahead_is_owned_push_available(self) -> None:
        # Git proves a commit no origin ref carries — the push is owned and
        # available, so the deferral costume would be a false quiet.
        assert (
            resolve_push_delivery_marker(
                None, BranchReachability.OWNED_AHEAD,
            )
            is Marker.OWNED_PUSH_AVAILABLE
        )

    def test_never_attempted_and_external_keeps_legacy_marker(self) -> None:
        # Nothing local to push, so deferring to Tend is the honest reading and
        # the legacy marker stays — the swap is narrow by design.
        assert (
            resolve_push_delivery_marker(
                None, BranchReachability.EXTERNAL,
            )
            is Marker.AWAITING_TEND_PUSH
        )

    def test_never_attempted_without_probe_fails_safe_to_legacy(self) -> None:
        # An unprobed branch must not assert an owned act git never confirmed.
        assert (
            resolve_push_delivery_marker(None, None)
            is Marker.AWAITING_TEND_PUSH
        )

    def test_reachability_defaults_to_none(self) -> None:
        # The parameter is optional; omitting it is the unprobed case.
        assert (
            resolve_push_delivery_marker(None) is Marker.AWAITING_TEND_PUSH
        )


class TestClassifyCommittedUnpushedPush:
    """The committed-but-unpushed push detector — the ``git push``-owed table.

    Distinct from the uncommitted-edit arm: fires only when the fix is already
    committed (``ahead_count > 0``), the tree is clean
    (``has_uncommitted_edit`` False), the blocker is AWAITING_PUSH (owned), and
    the gates are green. Any missing leg returns ``False``.
    """

    def test_true_only_for_committed_unpushed_gate_clean_awaiting_push(
        self,
    ) -> None:
        # The one owed act is the push: committed ahead of origin, clean tree,
        # AWAITING_PUSH blocker, gates green.
        assert (
            classify_committed_unpushed_push(
                blocker=MERGE_BLOCK_AWAITING_PUSH,
                ahead_count=1,
                has_uncommitted_edit=False,
                gates_pass=True,
            )
            is True
        )

    def test_case_and_whitespace_normalised(self) -> None:
        # The blocker is normalised before matching, mirroring
        # ``classify_blocker_ownership``.
        assert (
            classify_committed_unpushed_push(
                blocker="  awaiting_push  ",
                ahead_count=3,
                has_uncommitted_edit=False,
                gates_pass=True,
            )
            is True
        )

    def test_false_for_uncommitted_edit(self) -> None:
        # A dirty working tree is the uncommitted-edit arm's domain, not this one.
        assert (
            classify_committed_unpushed_push(
                blocker=MERGE_BLOCK_AWAITING_PUSH,
                ahead_count=1,
                has_uncommitted_edit=True,
                gates_pass=True,
            )
            is False
        )

    def test_false_for_red_gate(self) -> None:
        # A red head is never pushed — the actuator rewinds instead.
        assert (
            classify_committed_unpushed_push(
                blocker=MERGE_BLOCK_AWAITING_PUSH,
                ahead_count=1,
                has_uncommitted_edit=False,
                gates_pass=False,
            )
            is False
        )

    @pytest.mark.parametrize(
        "blocker",
        [
            MERGE_BLOCK_AWAITING_APPROVAL,
            MERGE_BLOCK_BEHIND_MAIN,
            MERGE_BLOCK_CONFLICTING,
            MERGE_BLOCK_CI_PENDING,
            MERGE_BLOCK_NONE,
            "",
        ],
    )
    def test_false_for_non_awaiting_push_blocker(self, blocker: str) -> None:
        # Only AWAITING_PUSH names an owned unpushed head; any other blocker
        # (external or a different owned gate) is not a bare push to fire here.
        assert (
            classify_committed_unpushed_push(
                blocker=blocker,
                ahead_count=1,
                has_uncommitted_edit=False,
                gates_pass=True,
            )
            is False
        )

    @pytest.mark.parametrize("ahead", [0, -1])
    def test_false_when_remote_caught_up(self, ahead: int) -> None:
        # A remote that has caught up reads zero ahead — nothing to push, so the
        # actuator self-extinguishes rather than re-firing.
        assert (
            classify_committed_unpushed_push(
                blocker=MERGE_BLOCK_AWAITING_PUSH,
                ahead_count=ahead,
                has_uncommitted_edit=False,
                gates_pass=True,
            )
            is False
        )


# ──────────────────────────────────────────────────────────────────────
# Keyed stand-down verdict tests
# ──────────────────────────────────────────────────────────────────────


class TestStandDownVerdict:
    """Persistence and exact (pr, head_sha) lookup of stand-down verdicts."""

    def test_record_and_find_round_trip(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A recorded verdict is found by the exact (pr, head_sha) key.
        data_dir = Path("/fake/data")
        record_stand_down(
            2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        verdict = find_stand_down(2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir))
        assert verdict == StandDownVerdict(
            pr=2638,
            head_sha="f6ad1ca3",
            recorded_at="2026-06-16T00:00:00+00:00",
        )

    def test_branch_ahead_field_round_trips(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The reachability fact stamped at authoring time round-trips through
        # append + read.
        data_dir = Path("/fake/data")
        record_stand_down(
            2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
            branch_ahead_of_origin=False,
        )
        verdict = find_stand_down(2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir))
        assert verdict is not None
        assert verdict.branch_ahead_of_origin is False
        # It is persisted as an explicit key on the row.
        raw = mem_fs[str(data_dir / "stand_down_verdicts.jsonl")]
        assert json.loads(raw.strip())["branch_ahead_of_origin"] is False

    def test_legacy_row_yields_none_branch_ahead(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A pre-existing row lacking the field deserializes with None, and the
        # default record path (no field passed) omits the key entirely.
        data_dir = Path("/fake/data")
        record_stand_down(
            2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        raw = mem_fs[str(data_dir / "stand_down_verdicts.jsonl")]
        assert "branch_ahead_of_origin" not in json.loads(raw.strip())
        verdict = find_stand_down(2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir))
        assert verdict is not None
        assert verdict.branch_ahead_of_origin is None

    def test_persisted_jsonl_format(self, mem_fs: dict[str, str]) -> None:
        # One JSONL line per verdict, with the mocked clock timestamp.
        data_dir = Path("/fake/data")
        record_stand_down(
            42, "deadbeef", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T12:00:00+00:00",
        )
        raw = mem_fs[str(data_dir / "stand_down_verdicts.jsonl")]
        assert json.loads(raw.strip()) == {
            "pr": 42,
            "head_sha": "deadbeef",
            "recorded_at": "2026-06-16T12:00:00+00:00",
        }

    def test_miss_when_head_sha_differs(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A different head SHA misses — the brake auto-releases on a new commit.
        data_dir = Path("/fake/data")
        record_stand_down(
            2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        assert find_stand_down(2638, "newsha99", JsonlStandDownVerdictLedger(data_dir)) is None

    def test_miss_when_pr_differs(self, mem_fs: dict[str, str]) -> None:
        # A verdict is keyed to its PR; another PR at the same SHA misses.
        data_dir = Path("/fake/data")
        record_stand_down(
            2638, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        assert find_stand_down(2640, "f6ad1ca3", JsonlStandDownVerdictLedger(data_dir)) is None

    def test_find_absent_file_returns_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No ledger yet — lookup returns None rather than raising.
        assert find_stand_down(1, "abc", JsonlStandDownVerdictLedger(Path("/fake/data"))) is None

    def test_most_recent_match_wins(self, mem_fs: dict[str, str]) -> None:
        # A re-authored verdict (same key) supersedes the earlier one.
        data_dir = Path("/fake/data")
        record_stand_down(
            7, "sha7", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        record_stand_down(
            7, "sha7", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T06:00:00+00:00",
        )
        verdict = find_stand_down(7, "sha7", JsonlStandDownVerdictLedger(data_dir))
        assert verdict is not None
        assert verdict.recorded_at == "2026-06-16T06:00:00+00:00"

    def test_record_ignores_non_positive_pr(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A verdict that could never be looked up is not written.
        data_dir = Path("/fake/data")
        record_stand_down(0, "abc", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        record_stand_down(-3, "abc", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        assert str(data_dir / "stand_down_verdicts.jsonl") not in mem_fs

    def test_record_ignores_empty_head_sha(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An empty SHA would spuriously match an unresolvable head — not written.
        data_dir = Path("/fake/data")
        record_stand_down(5, "", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        assert str(data_dir / "stand_down_verdicts.jsonl") not in mem_fs

    def test_find_empty_head_sha_returns_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An empty head SHA never matches a recorded verdict.
        data_dir = Path("/fake/data")
        record_stand_down(5, "abc", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        assert find_stand_down(5, "", JsonlStandDownVerdictLedger(data_dir)) is None

    def test_default_clock_stamps_timestamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        # With no injected clock, the current UTC time is stamped (parseable).
        data_dir = Path("/fake/data")
        record_stand_down(9, "sha9", JsonlStandDownVerdictLedger(data_dir))
        verdict = find_stand_down(9, "sha9", JsonlStandDownVerdictLedger(data_dir))
        assert verdict is not None
        assert verdict.recorded_at  # non-empty ISO timestamp

    def test_malformed_and_mistyped_lines_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Corrupt JSON, non-dict records, mistyped fields, and blank lines are
        # all tolerated; only the well-formed matching record is returned.
        data_dir = Path("/fake/data")
        path = str(data_dir / "stand_down_verdicts.jsonl")
        mem_fs[path] = (
            "not json {\n"
            + "\n"  # blank line skipped
            + json.dumps([1, 2, 3]) + "\n"
            + json.dumps({"pr": "x", "head_sha": "s", "recorded_at": "t"}) + "\n"
            + json.dumps({"pr": True, "head_sha": "s", "recorded_at": "t"}) + "\n"
            + json.dumps({"pr": 11, "head_sha": 99, "recorded_at": "t"}) + "\n"
            + json.dumps({"pr": 11, "head_sha": "s", "recorded_at": "ok"}) + "\n"
        )
        verdict = find_stand_down(11, "s", JsonlStandDownVerdictLedger(data_dir))
        assert verdict == StandDownVerdict(
            pr=11, head_sha="s", recorded_at="ok",
        )

    def test_find_os_error_returns_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An OSError opening the ledger fails closed to None rather than raising.
        data_dir = Path("/fake/data")
        record_stand_down(11, "s", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        with patch("builtins.open", side_effect=OSError("disk error")):
            assert find_stand_down(11, "s", JsonlStandDownVerdictLedger(data_dir)) is None

    def test_release_voids_live_verdict(self, mem_fs: dict[str, str]) -> None:
        # A head-agnostic release row voids an earlier matched verdict (proposal
        # 091) — the exact-SHA lookup would otherwise still match.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        assert find_stand_down(2884, "sha1", JsonlStandDownVerdictLedger(data_dir)) is not None
        release_stand_downs(2884, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T01:00:00+00:00")
        assert find_stand_down(2884, "sha1", JsonlStandDownVerdictLedger(data_dir)) is None

    def test_verdict_after_release_matches_again(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A verdict recorded on a later line than the release still matches —
        # last-write-wins is preserved.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        release_stand_downs(2884, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T01:00:00+00:00")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T02:00:00+00:00",
        )
        verdict = find_stand_down(2884, "sha1", JsonlStandDownVerdictLedger(data_dir))
        assert verdict is not None
        assert verdict.recorded_at == "2026-06-16T02:00:00+00:00"

    def test_release_leaves_other_pr_verdict_intact(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A release is keyed to its PR; another PR's verdict is untouched.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        record_stand_down(
            2900, "sha2", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        release_stand_downs(2884, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T01:00:00+00:00")
        assert find_stand_down(2884, "sha1", JsonlStandDownVerdictLedger(data_dir)) is None
        assert find_stand_down(2900, "sha2", JsonlStandDownVerdictLedger(data_dir)) is not None

    def test_release_persisted_format(self, mem_fs: dict[str, str]) -> None:
        # A release row is one JSONL line carrying only ``pr`` + ``released_at``.
        data_dir = Path("/fake/data")
        release_stand_downs(7, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T03:00:00+00:00")
        raw = mem_fs[str(data_dir / "stand_down_verdicts.jsonl")]
        assert json.loads(raw.strip()) == {
            "pr": 7,
            "released_at": "2026-06-16T03:00:00+00:00",
        }

    def test_release_ignores_non_positive_pr(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A release with no PR key to void writes nothing.
        data_dir = Path("/fake/data")
        release_stand_downs(0, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        release_stand_downs(-2, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        assert str(data_dir / "stand_down_verdicts.jsonl") not in mem_fs

    def test_release_default_clock_stamps_timestamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        # With no injected clock, a parseable UTC timestamp is stamped.
        data_dir = Path("/fake/data")
        record_stand_down(9, "sha9", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        release_stand_downs(9, JsonlStandDownVerdictLedger(data_dir))
        assert find_stand_down(9, "sha9", JsonlStandDownVerdictLedger(data_dir)) is None

    def test_release_row_does_not_inflate_written_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A release row carries no ``head_sha``/``recorded_at``, so it is not
        # counted among the verdicts ``measure_standdown_production`` reports.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        release_stand_downs(2884, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T01:00:00+00:00")
        _candidates, written = measure_standdown_production([], JsonlStandDownVerdictLedger(data_dir))
        assert written == 1

    # ── has_live_stand_down: head-agnostic liveness (proposal 103, store 4) ──

    def test_has_live_stand_down_true_after_recorded_verdict(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A recorded verdict makes the PR's suppression live, at any head.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        assert has_live_stand_down(2884, JsonlStandDownVerdictLedger(data_dir)) is True

    def test_has_live_stand_down_is_head_agnostic(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Unlike find_stand_down, liveness ignores the head SHA: a verdict at one
        # SHA reads live even though find_stand_down at a different SHA misses.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        assert find_stand_down(2884, "otherSHA", JsonlStandDownVerdictLedger(data_dir)) is None
        assert has_live_stand_down(2884, JsonlStandDownVerdictLedger(data_dir)) is True

    def test_has_live_stand_down_false_after_release(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A later release row voids the verdict head-agnostically.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        release_stand_downs(2884, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T01:00:00+00:00")
        assert has_live_stand_down(2884, JsonlStandDownVerdictLedger(data_dir)) is False

    def test_has_live_stand_down_true_when_re_recorded_after_release(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A verdict recorded on a later line than the release re-arms the brake —
        # last-write-wins, mirroring find_stand_down's line-order semantics.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        release_stand_downs(2884, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T01:00:00+00:00")
        record_stand_down(
            2884, "sha2", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T02:00:00+00:00",
        )
        assert has_live_stand_down(2884, JsonlStandDownVerdictLedger(data_dir)) is True

    def test_has_live_stand_down_false_for_other_pr(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A verdict is keyed to its PR; another PR reads as not live.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        assert has_live_stand_down(2900, JsonlStandDownVerdictLedger(data_dir)) is False

    def test_has_live_stand_down_release_for_other_pr_leaves_live(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A release row keyed to a *different* PR does not void this PR's live
        # verdict — the release branch is PR-scoped.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        release_stand_downs(2900, JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T01:00:00+00:00")
        assert has_live_stand_down(2884, JsonlStandDownVerdictLedger(data_dir)) is True

    def test_has_live_stand_down_false_for_absent_file(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No ledger yet — the predicate returns False rather than raising.
        assert has_live_stand_down(2884, JsonlStandDownVerdictLedger(Path("/fake/data"))) is False

    def test_has_live_stand_down_false_for_non_positive_pr(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A non-positive PR carries no key to match, so it is never live.
        data_dir = Path("/fake/data")
        record_stand_down(
            2884, "sha1", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00",
        )
        assert has_live_stand_down(0, JsonlStandDownVerdictLedger(data_dir)) is False
        assert has_live_stand_down(-3, JsonlStandDownVerdictLedger(data_dir)) is False

    def test_has_live_stand_down_tolerates_corrupted_tail(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Corrupt JSON, non-dict records, mistyped/bool ``pr`` fields, and blank
        # lines are all skipped; the well-formed matching verdict still reads
        # live — corrupted-tail tolerance mirrors find_stand_down.
        data_dir = Path("/fake/data")
        path = str(data_dir / "stand_down_verdicts.jsonl")
        mem_fs[path] = (
            "not json {\n"
            + "\n"  # blank line skipped
            + json.dumps([1, 2, 3]) + "\n"
            + json.dumps({"pr": "x", "head_sha": "s", "recorded_at": "t"}) + "\n"
            + json.dumps({"pr": True, "head_sha": "s", "recorded_at": "t"}) + "\n"
            + json.dumps({"pr": 11, "head_sha": "s", "recorded_at": "ok"}) + "\n"
        )
        assert has_live_stand_down(11, JsonlStandDownVerdictLedger(data_dir)) is True

    def test_has_live_stand_down_os_error_returns_false(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An OSError opening the ledger fails closed to False rather than raising.
        data_dir = Path("/fake/data")
        record_stand_down(11, "s", JsonlStandDownVerdictLedger(data_dir), now="2026-06-16T00:00:00+00:00")
        with patch("builtins.open", side_effect=OSError("disk error")):
            assert has_live_stand_down(11, JsonlStandDownVerdictLedger(data_dir)) is False


class TestLeadingSameShaStreak:
    """The shared leading-same-SHA streak helper (#2865).

    One measure of the stalled-lineage streak reused by the pure predicate, its
    production counter, and the agent's observation-backed fallback.
    """

    def test_empty_laps_is_zero(self) -> None:
        assert leading_same_sha_streak([], "a" * 40) == 0

    def test_all_matching_counts_all(self) -> None:
        assert leading_same_sha_streak(["a", "a", "a"], "a") == 3

    def test_breaks_at_first_differing_sha(self) -> None:
        # Two leading matches, then a differing SHA ends the run — trailing
        # matches past the break are not counted.
        assert leading_same_sha_streak(["a", "a", "b", "a"], "a") == 2

    def test_leading_mismatch_is_zero(self) -> None:
        assert leading_same_sha_streak(["b", "a", "a"], "a") == 0

    def test_empty_head_matches_empty_entries(self) -> None:
        # An empty head (no recorded SHA) matches empty lap entries — callers
        # that must exclude a keyless head guard it before calling.
        assert leading_same_sha_streak(["", "", "x"], "") == 2


class TestMechanicalStandDownVerdict:
    """The pure mechanical chosen-stand-down predicate.

    Fires a :class:`StandDownVerdict` only when the PR is git-unlanded, the
    head SHA is unchanged across ``>= threshold`` prior ``partial`` laps, and
    no unserviced review thread remains. Whether the remaining blocker is
    clearable is not this predicate's question — clearability is governed by
    the caller's salvage oracle at the write site. ``now`` is injected for a
    deterministic timestamp; ``is_pr_merged`` is injected so the predicate
    never shells git out in a unit test.
    """

    _NOT_MERGED = staticmethod(lambda _pr: False)
    _SHA = "a" * 40

    def test_fires_on_stalled_thread_clear_unlanded(self) -> None:
        # All conditions hold: two same-SHA partial laps, no unserviced
        # thread, git-unlanded => a verdict is authored regardless of what
        # blocker the caller observed — the clearability decision has moved
        # out of this function, into the caller's salvage oracle.
        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            [self._SHA, self._SHA],
            False,
            2,
            is_pr_merged=self._NOT_MERGED,
            now="2026-06-26T00:00:00+00:00",
        )
        assert verdict == StandDownVerdict(
            pr=2819,
            head_sha=self._SHA,
            recorded_at="2026-06-26T00:00:00+00:00",
        )

    def test_none_on_changed_sha_auto_release(self) -> None:
        # The most-recent lap is on a different (advanced) head SHA, so the
        # leading run equal to the current head is 1 < threshold => None.
        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            ["b" * 40, self._SHA],
            False,
            2,
            is_pr_merged=self._NOT_MERGED,
        )
        assert verdict is None

    def test_none_on_unserviced_thread(self) -> None:
        # An unserviced review thread is LLM-only work — never suppressed.
        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            [self._SHA, self._SHA],
            True,
            2,
            is_pr_merged=self._NOT_MERGED,
        )
        assert verdict is None

    def test_none_below_threshold(self) -> None:
        # A single same-SHA partial lap is below the threshold => None.
        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            [self._SHA],
            False,
            2,
            is_pr_merged=self._NOT_MERGED,
        )
        assert verdict is None

    def test_none_when_git_shows_merged(self) -> None:
        # git-merge-truth shows the PR merged => the predicate stands down on
        # standing down (nothing to suppress); the oracle is consulted last.
        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            [self._SHA, self._SHA],
            False,
            2,
            is_pr_merged=lambda _pr: True,
        )
        assert verdict is None

    def test_oracle_not_consulted_until_cheap_conditions_pass(self) -> None:
        # The git oracle must not run when a cheap condition already fails —
        # here an unserviced thread short-circuits before any merge-truth read.
        def _boom(_pr: int) -> bool:
            raise AssertionError("is_pr_merged must not be called")

        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            [self._SHA, self._SHA],
            True,
            2,
            is_pr_merged=_boom,
        )
        assert verdict is None

    def test_none_on_non_positive_threshold(self) -> None:
        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            [self._SHA, self._SHA],
            False,
            0,
            is_pr_merged=self._NOT_MERGED,
        )
        assert verdict is None

    def test_none_on_non_positive_pr(self) -> None:
        verdict = mechanical_standdown_verdict(
            0,
            self._SHA,
            [self._SHA, self._SHA],
            False,
            2,
            is_pr_merged=self._NOT_MERGED,
        )
        assert verdict is None

    def test_none_on_empty_head_sha(self) -> None:
        verdict = mechanical_standdown_verdict(
            2819,
            "",
            ["", ""],
            False,
            2,
            is_pr_merged=self._NOT_MERGED,
        )
        assert verdict is None

    def test_default_oracle_used_when_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        # With no injected oracle the lazy-imported git default is consulted;
        # patch it to keep the unit test off real git.
        with patch(
            "maestro.audit_checks_phantom._git_pr_merged_default", return_value=False,
        ) as oracle:
            verdict = mechanical_standdown_verdict(
                2819,
                self._SHA,
                [self._SHA, self._SHA],
                False,
                2,
                now="2026-06-26T00:00:00+00:00",
            )
        oracle.assert_called_once_with(2819)
        assert verdict is not None
        assert verdict.pr == 2819

    def test_default_clock_stamps_timestamp(self) -> None:
        # With no injected clock a parseable UTC timestamp is stamped.
        verdict = mechanical_standdown_verdict(
            2819,
            self._SHA,
            [self._SHA, self._SHA],
            False,
            2,
            is_pr_merged=self._NOT_MERGED,
        )
        assert verdict is not None
        assert verdict.recorded_at  # non-empty ISO timestamp


# ──────────────────────────────────────────────────────────────────────
# STAND-DOWN-PRODUCTION counter tests (#2865)
# ──────────────────────────────────────────────────────────────────────


class TestMeasureStandDownProduction:
    """Candidates-vs-written counter for the mechanical chosen-stand-down."""

    _DIR = Path("/fake/data")

    @staticmethod
    def _lap(
        pr: int | None,
        *,
        outcome: str = "partial",
        sha: str = "a" * 40,
    ) -> dict[str, object]:
        goal = (
            f"Remediate PR #{pr}: address comments"
            if pr is not None
            else "Build a sensor"
        )
        return {"goal": goal, "outcome": outcome, "pr_head_sha": sha}

    def test_zero_candidates_returns_empty(self, mem_fs: dict[str, str]) -> None:
        # A single same-SHA partial lap is below the stall threshold => no
        # candidate, and the formatter injects nothing.
        tasks = [self._lap(100)]
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (0, 0)
        assert format_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == ""

    def test_candidate_counted_and_line_rendered(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Two same-SHA partial laps reach the threshold (one candidate); the
        # ledger holds one matching verdict.
        tasks = [self._lap(100), self._lap(100)]
        record_stand_down(
            100, "a" * 40, JsonlStandDownVerdictLedger(self._DIR), now="2026-06-16T00:00:00+00:00",
        )
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (1, 1)
        line = format_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR))
        assert line.startswith("STAND-DOWN-PRODUCTION: 1/1")

    def test_gap_when_candidate_has_no_written_verdict(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A qualifying candidate with an empty ledger surfaces the starved gap.
        tasks = [self._lap(100), self._lap(100)]
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (1, 0)
        assert "0/1" in format_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR))

    def test_non_partial_head_closes_the_run(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Oldest-first two same-SHA partials THEN a most-recent success for the
        # same PR: the non-partial head closes the trailing run, so the older
        # partials never count — not a candidate.
        tasks = [
            self._lap(100),
            self._lap(100),
            self._lap(100, outcome="success"),
        ]
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (0, 0)

    def test_streak_break_on_differing_sha(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An older differing-SHA partial under two same-SHA partials: the leading
        # run still reaches the threshold (the streak breaks above it) => one
        # candidate.
        tasks = [
            self._lap(100, sha="b" * 40),
            self._lap(100),
            self._lap(100),
        ]
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (1, 0)

    def test_empty_head_sha_skipped(self, mem_fs: dict[str, str]) -> None:
        # A partial lap missing its head SHA cannot key a verdict => skipped.
        tasks: list[dict[str, object]] = [
            {"goal": "Remediate PR #100: address comments", "outcome": "partial"},
            {"goal": "Remediate PR #100: address comments", "outcome": "partial"},
        ]
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (0, 0)

    def test_non_remediation_and_malformed_goals_ignored(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A non-remediation goal (no PR number) and a non-string goal are both
        # skipped without error; the genuine lineage still counts.
        tasks: list[dict[str, object]] = [
            self._lap(None),
            {"goal": None, "outcome": "partial", "pr_head_sha": "a" * 40},
            self._lap(100),
            self._lap(100),
        ]
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (1, 0)

    def test_corrupted_ledger_rows_tolerated(
        self, mem_fs: dict[str, str],
    ) -> None:
        # One valid verdict row plus a corrupt line, a non-dict line, a bool pr,
        # and a non-string head_sha: only the valid row counts as written.
        record_stand_down(
            100, "a" * 40, JsonlStandDownVerdictLedger(self._DIR), now="2026-06-16T00:00:00+00:00",
        )
        path = str(self._DIR / "stand_down_verdicts.jsonl")
        mem_fs[path] += "not-json{\n"
        mem_fs[path] += json.dumps([1, 2, 3]) + "\n"
        mem_fs[path] += json.dumps(
            {"pr": True, "head_sha": "x", "recorded_at": "t"},
        ) + "\n"
        mem_fs[path] += json.dumps(
            {"pr": 7, "head_sha": 9, "recorded_at": "t"},
        ) + "\n"
        tasks = [self._lap(100), self._lap(100)]
        assert measure_standdown_production(tasks, JsonlStandDownVerdictLedger(self._DIR)) == (1, 1)

    def test_written_count_os_error_propagates(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The measurement read is deliberately fail-loud (unlike the fail-closed
        # find/has_live reads): a genuinely unreadable ledger propagates the
        # OSError rather than silently reporting zero written, matching the
        # pre-seam body and the SQLite adapter (proposal 106 flag parity).
        record_stand_down(
            100, "a" * 40, JsonlStandDownVerdictLedger(self._DIR),
            now="2026-06-16T00:00:00+00:00",
        )
        tasks = [self._lap(100), self._lap(100)]
        with (
            patch("builtins.open", side_effect=OSError("disk error")),
            pytest.raises(OSError, match="disk error"),
        ):
            measure_standdown_production(
                tasks, JsonlStandDownVerdictLedger(self._DIR),
            )


# ──────────────────────────────────────────────────────────────────────
# Verdict-needs-fetch gate tests
# ──────────────────────────────────────────────────────────────────────


class TestVersionArchiveHasLanding:
    """The host-independent version-archive corroboration reader.

    The reader now takes an already-loaded iterable of ``VersionRecord``-shaped
    rows (the caller loads them through the injected backend), so it is a pure
    predicate with no file I/O — the corrupt-row / OSError tolerance moved to the
    backend's ``load_all`` (covered in ``test_state_store``/``test_memory``).
    """

    @staticmethod
    def _rec(plan_id: str) -> SimpleNamespace:
        # A minimal ``VersionRecord``-shaped row; the reader duck-types on
        # ``plan_id`` via ``getattr``, so only that attribute matters here.
        return SimpleNamespace(plan_id=plan_id)

    def test_matching_plan_id_is_a_landing(self) -> None:
        # A VersionRecord (written only on an actual landing) whose plan_id
        # matches is a merge-truth signal already loaded from local disk.
        records = [self._rec("plan-other"), self._rec("plan-3179")]
        assert version_archive_has_landing("plan-3179", records) is True

    def test_absent_plan_id_is_not_a_landing(self) -> None:
        # No record for the plan → no corroboration (stays unsettled upstream).
        records = [self._rec("plan-other")]
        assert version_archive_has_landing("plan-3179", records) is False

    def test_empty_plan_id_never_matches(self) -> None:
        # An empty plan_id carries no key to match on, even with records present.
        records = [self._rec("")]
        assert version_archive_has_landing("", records) is False

    def test_empty_records_is_not_a_landing(self) -> None:
        # An empty archive → no corroboration.
        assert version_archive_has_landing("plan-3179", []) is False

    def test_record_lacking_plan_id_is_skipped(self) -> None:
        # A row without a ``plan_id`` attribute yields ``None`` via ``getattr``
        # and never matches; a well-formed matching row still returns True.
        records = [SimpleNamespace(version_id="v1"), self._rec("plan-3181")]
        assert version_archive_has_landing("plan-3181", records) is True
        assert version_archive_has_landing("plan-absent", records) is False


class TestFetchTruthMarker:
    """Round-trip and tolerance tests for the per-task fetch-truth marker."""

    def test_round_trips_through_mem_fs(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A recorded marker is found by has_fetch_truth for the same task_id.
        data_dir = Path("/fake/data")
        assert has_fetch_truth("task-a", data_dir) is False
        record_fetch_truth("task-a", 2647, True, data_dir)
        assert has_fetch_truth("task-a", data_dir) is True
        # A different task_id is not matched by an unrelated marker.
        assert has_fetch_truth("task-b", data_dir) is False

    def test_record_persists_fields(self, mem_fs: dict[str, str]) -> None:
        # The marker line carries task_id, pr_number, and the ancestry verdict.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-x", 2696, False, data_dir)
        path = str(data_dir / "fetch_truth_markers.jsonl")
        parsed = json.loads(mem_fs[path].strip())
        assert parsed == {
            "task_id": "task-x",
            "pr_number": 2696,
            "is_ancestor": False,
        }

    def test_record_ignores_empty_task_id(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An empty task_id carries no lookup key — nothing is written.
        data_dir = Path("/fake/data")
        record_fetch_truth("", 1, True, data_dir)
        assert str(data_dir / "fetch_truth_markers.jsonl") not in mem_fs

    def test_has_fetch_truth_empty_task_id(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An empty task_id never matches, even with markers present.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-a", 1, True, data_dir)
        assert has_fetch_truth("", data_dir) is False

    def test_has_fetch_truth_missing_file(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No ledger file → no marker.
        assert has_fetch_truth("task-a", Path("/fake/data")) is False

    def test_has_fetch_truth_malformed_lines_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Corrupt JSON, non-dict records, and blank lines are tolerated; only a
        # well-formed matching record returns True.
        data_dir = Path("/fake/data")
        path = str(data_dir / "fetch_truth_markers.jsonl")
        mem_fs[path] = (
            "not json {\n"
            + "\n"
            + json.dumps([1, 2, 3]) + "\n"
            + json.dumps({"task_id": "other", "pr_number": 1}) + "\n"
            + json.dumps({"task_id": "task-z", "pr_number": 2}) + "\n"
        )
        assert has_fetch_truth("task-z", data_dir) is True
        assert has_fetch_truth("absent", data_dir) is False

    def test_has_fetch_truth_os_error_returns_false(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An OSError reading the ledger fails closed to False (gate downgrades).
        data_dir = Path("/fake/data")
        record_fetch_truth("task-a", 1, True, data_dir)
        with patch("builtins.open", side_effect=OSError("disk error")):
            assert has_fetch_truth("task-a", data_dir) is False

    def test_record_fetch_truth_persists_head_sha(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A non-empty head SHA round-trips as a fourth field in the JSONL record.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-h", 2864, False, data_dir, head_sha="abc1234")
        path = str(data_dir / "fetch_truth_markers.jsonl")
        parsed = json.loads(mem_fs[path].strip())
        assert parsed == {
            "task_id": "task-h",
            "pr_number": 2864,
            "is_ancestor": False,
            "head_sha": "abc1234",
        }

    def test_record_fetch_truth_omits_empty_head_sha(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An empty head SHA keeps the legacy three-field shape (readers tolerate
        # the missing field) — the marker never carries an empty ``head_sha``.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-l", 2900, True, data_dir)
        path = str(data_dir / "fetch_truth_markers.jsonl")
        parsed = json.loads(mem_fs[path].strip())
        assert "head_sha" not in parsed


class TestFetchTruthRelap:
    """Tests for the fetch-truth lap-delta reader (owned-relap divert #2864)."""

    def test_detects_unchanged_unlanded_marker(
        self, mem_fs: dict[str, str],
    ) -> None:
        # True only when a prior is_ancestor=false marker matches the same
        # (pr_number, head_sha); every other shape falls through to False.
        data_dir = Path("/fake/data")
        # A prior lap (a *different* task) proved (2864, sha-x) unlanded.
        record_fetch_truth("task-prior", 2864, False, data_dir, head_sha="sha-x")
        assert fetch_truth_relap(2864, "sha-x", data_dir) is True
        # A changed head SHA never matches the recorded one.
        assert fetch_truth_relap(2864, "sha-y", data_dir) is False
        # A different PR number never matches.
        assert fetch_truth_relap(2999, "sha-x", data_dir) is False
        # An is_ancestor=true marker (already landed) is not a re-lap signal.
        record_fetch_truth("task-landed", 2900, True, data_dir, head_sha="sha-z")
        assert fetch_truth_relap(2900, "sha-z", data_dir) is False
        # An empty head SHA (unresolvable branch) never diverts.
        assert fetch_truth_relap(2864, "", data_dir) is False

    def test_missing_file_returns_false(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No ledger file → no re-lap signal.
        assert fetch_truth_relap(2864, "sha-x", Path("/fake/data")) is False

    def test_legacy_marker_without_head_sha_ignored(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A legacy marker lacking the head_sha field cannot match any query SHA.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-legacy", 2864, False, data_dir)
        assert fetch_truth_relap(2864, "sha-x", data_dir) is False

    def test_malformed_lines_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Corrupt JSON, non-dict records, and blank lines are tolerated; only a
        # well-formed matching record returns True (mirrors has_fetch_truth).
        data_dir = Path("/fake/data")
        path = str(data_dir / "fetch_truth_markers.jsonl")
        mem_fs[path] = (
            "not json {\n"
            + "\n"
            + json.dumps([1, 2, 3]) + "\n"
            + json.dumps(
                {"pr_number": 2864, "head_sha": "sha-x", "is_ancestor": False},
            ) + "\n"
        )
        assert fetch_truth_relap(2864, "sha-x", data_dir) is True

    def test_os_error_returns_false(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An OSError reading the ledger fails closed to False.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-a", 2864, False, data_dir, head_sha="sha-x")
        with patch("builtins.open", side_effect=OSError("disk error")):
            assert fetch_truth_relap(2864, "sha-x", data_dir) is False


class TestGateRemediationVerdict:
    """Tests for the verdict-needs-fetch downgrade gate."""

    def test_no_marker_downgrades_to_fetch_missing(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A success carrying the on-branch costume with no fetch-truth marker is
        # forced to a [fetch-missing] partial.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-1",
            "Resolved on branch, awaiting tend push — PR #2647 not yet landed.",
            "success",
            data_dir,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fetch-missing] ")

    def test_owned_push_verdict_without_fetch_downgrades(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An owned-push verdict (proposal 098) is a claim about where the commit
        # is, so it owes the same fetch-truth proof as the two push markers it
        # replaces on the non-delivery arms: without a recorded fetch the
        # verdict spoke before git did.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-op",
            "[owned-push-available] PR #2647 branch is ahead of origin.",
            "success",
            data_dir,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fetch-missing] ")

    def test_push_unconfirmed_verdict_without_fetch_downgrades(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A push-unconfirmed verdict (proposal 099) makes the same on-branch,
        # un-pushed claim as OWNED_PUSH_AVAILABLE, so it owes the same fetch-
        # truth proof; before the marker split a no-op push stamped
        # DELIVERED_AWAITING_MERGE (also required here). Without a recorded
        # fetch the verdict spoke before git did.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-pu",
            "[push-unconfirmed] PR #2647 branch: push exited 0 but origin "
            "did not advance.",
            "success",
            data_dir,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fetch-missing] ")

    def test_marker_present_passes_unchanged(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A recorded marker proves git spoke first — the verdict passes intact.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-2", 2647, True, data_dir)
        text = "Resolved on branch, awaiting tend push — PR #2647 not landed."
        outcome, verdict = gate_remediation_verdict(
            "task-2", text, "partial", data_dir,
        )
        assert outcome == "partial"
        assert verdict == text

    def test_is_ancestor_marker_permits_success(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The is_ancestor=True marker permits a success stamp to survive.
        data_dir = Path("/fake/data")
        record_fetch_truth("task-3", 2647, True, data_dir)
        text = "Resolved on branch, awaiting tend push — PR #2647."
        outcome, verdict = gate_remediation_verdict(
            "task-3", text, "success", data_dir,
        )
        assert outcome == "success"
        assert verdict == text

    def test_conflicting_narrative_downgrades(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The CONFLICTING merge-block reason code also requires fetch-truth.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-4",
            "Resolved (merge blocker: CONFLICTING).",
            "partial",
            data_dir,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fetch-missing] ")

    def test_awaiting_push_narrative_downgrades(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The AWAITING_PUSH reason code also requires fetch-truth.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-5",
            "Resolved (merge blocker: AWAITING_PUSH).",
            "success",
            data_dir,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fetch-missing] ")

    def test_no_narrative_passes_without_marker(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A verdict carrying none of the fetch-required narratives is untouched
        # even with no marker — the gate only guards on-branch landing claims.
        data_dir = Path("/fake/data")
        text = "Fixed the failing test and committed the fix."
        outcome, verdict = gate_remediation_verdict(
            "task-6", text, "success", data_dir,
        )
        assert outcome == "success"
        assert verdict == text

    def test_failure_outcome_passes_unchanged(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Only success/partial stamps are gated; a failure is left alone.
        data_dir = Path("/fake/data")
        text = "awaiting tend push"
        outcome, verdict = gate_remediation_verdict(
            "task-7", text, "failure", data_dir,
        )
        assert outcome == "failure"
        assert verdict == text

    def test_empty_verdict_text_passes_unchanged(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An empty verdict carries no narrative to gate.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-8", "", "success", data_dir,
        )
        assert outcome == "success"
        assert verdict == ""

    def test_owned_awaiting_push_no_fork_marker_forces_fork_missing(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Criterion 2: an owned AWAITING_PUSH verdict with no obligation-fork
        # marker is forced to a [fork-missing] partial — the rev-parse compare
        # was never run, so the deferral cannot know whether the push is owed.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-fm",
            "Resolved on branch, awaiting review/merge — PR #3208.",
            "success",
            data_dir,
            owned_awaiting_push=True,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fork-missing] ")

    def test_owned_awaiting_push_head_ahead_coerces_to_push_owed(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Criterion 3: a fork marker proving HEAD *genuinely ahead* of
        # origin/<branch> (differing SHAs AND origin an ancestor of HEAD) on an
        # external-deferral verdict is coerced to [push-owed] partial whose
        # summary names the owed push.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "task-po", 3208, "aaaaaaaaaaaa", "bbbbbbbbbbbb",
            "maestro/evolve/x", data_dir, remote_is_ancestor=True,
        )
        outcome, verdict = gate_remediation_verdict(
            "task-po",
            "Resolved on branch, awaiting tend push — PR #3208.",
            "partial",
            data_dir,
            owned_awaiting_push=True,
        )
        assert outcome == "partial"
        assert verdict.startswith("[push-owed] ")
        # The summary names the owed push: the branch and that it is mine.
        assert "origin/maestro/evolve/x" in verdict
        assert "push is\nmine to fire" in verdict or "push is mine to fire" in verdict

    def test_owned_awaiting_push_head_diverged_does_not_claim_push_owed(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A fork whose SHAs differ but whose origin/<branch> is NOT an ancestor of
        # HEAD (diverged / behind — e.g. a stale local PR branch Tend advanced
        # remotely) must NOT emit the "HEAD is ahead ... the push is mine to fire"
        # [push-owed] language: a plain push would non-fast-forward reject, a
        # reconcile is owed. head_ahead is False, so it falls through to the
        # equal-SHA (fetch-gated) pass-through arm, not the push-owed arm.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "task-div", 3208, "aaaaaaaaaaaa", "bbbbbbbbbbbb",
            "maestro/evolve/x", data_dir, remote_is_ancestor=False,
        )
        fork = get_obligation_fork("task-div", data_dir)
        assert fork is not None
        assert fork.head_ahead is False
        # No fetch-truth marker → the equal-SHA arm downgrades to [fetch-missing],
        # never [push-owed] with the factually-inverted "is ahead" wording.
        text = "Resolved on branch, awaiting merge — PR #3208."
        outcome, verdict = gate_remediation_verdict(
            "task-div", text, "partial", data_dir, owned_awaiting_push=True,
        )
        assert outcome == "partial"
        assert not verdict.startswith("[push-owed] ")
        assert verdict.startswith("[fetch-missing] ")

    def test_push_owed_keys_on_ancestry_not_bare_inequality(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The corrected behaviour, isolated to the single variable that flips it:
        # two forks with the *same* differing SHAs and *same* fetch-truth backing,
        # differing only in whether origin/<branch> is an ancestor of HEAD.
        #   - genuinely ahead (remote_is_ancestor=True)  -> [push-owed] fires
        #   - diverged/behind (remote_is_ancestor=False) -> [push-owed] must NOT
        #     fire; with fetch-truth present it passes through unchanged (a
        #     reconcile is owed, not the "push is mine to fire" claim).
        # A bare SHA inequality would (wrongly) fire push-owed in both arms.
        data_dir = Path("/fake/data")
        text = "Resolved on branch, awaiting tend push — PR #3208."

        # Genuinely-ahead fork + fetch-truth: push-owed still fires.
        record_obligation_fork_truth(
            "task-ahead", 3208, "aaaaaaaaaaaa", "bbbbbbbbbbbb",
            "maestro/evolve/x", data_dir, remote_is_ancestor=True,
        )
        record_fetch_truth("task-ahead", 3208, False, data_dir)
        ahead_outcome, ahead_verdict = gate_remediation_verdict(
            "task-ahead", text, "partial", data_dir, owned_awaiting_push=True,
        )
        assert ahead_outcome == "partial"
        assert ahead_verdict.startswith("[push-owed] ")

        # Same differing SHAs, same fetch-truth, but origin is NOT an ancestor of
        # HEAD (diverged/behind): push-owed does NOT fire — fetch-truth lets the
        # deferral pass through unchanged instead of a factually-inverted claim.
        record_obligation_fork_truth(
            "task-behind", 3208, "aaaaaaaaaaaa", "bbbbbbbbbbbb",
            "maestro/evolve/x", data_dir, remote_is_ancestor=False,
        )
        record_fetch_truth("task-behind", 3208, False, data_dir)
        behind_outcome, behind_verdict = gate_remediation_verdict(
            "task-behind", text, "partial", data_dir, owned_awaiting_push=True,
        )
        assert not behind_verdict.startswith("[push-owed] ")
        assert behind_outcome == "partial"
        assert behind_verdict == text

    def test_owned_awaiting_push_head_equal_passes_when_fetch_truth_recorded(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Criterion 4: a fork marker showing HEAD == origin/<branch> (the push is
        # discharged) leaves the verdict unchanged — the remaining wait is
        # genuinely external — but only when a fetch-truth marker proves the
        # origin/<branch> read was fresh this turn (not a stale remote ref).
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "task-eq", 3208, "cccccccccccc", "cccccccccccc",
            "maestro/evolve/y", data_dir,
        )
        record_fetch_truth("task-eq", 3208, False, data_dir)
        text = "Delivered to PR, awaiting review/merge — PR #3208."
        outcome, verdict = gate_remediation_verdict(
            "task-eq", text, "partial", data_dir, owned_awaiting_push=True,
        )
        assert outcome == "partial"
        assert verdict == text

    def test_owned_awaiting_push_head_equal_without_fetch_truth_downgraded(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The equal-SHA arm must not trust the unenforced "the fork's rev-parse
        # already fetched" assumption: recording the fork marker stores whatever
        # SHAs the caller passed and never proves a ``git fetch`` ran this turn,
        # and ``git rev-parse origin/<branch>`` reads a local remote-tracking ref
        # that can be stale. So an owned external-deferral verdict whose fork
        # shows HEAD == origin/<branch> but carries NO fetch-truth marker is
        # downgraded to a [fetch-missing] partial, exactly like the fetch floor —
        # a status word cannot stand on a possibly-stale ref with no this-turn
        # fetch behind it.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "task-eq-stale", 3208, "cccccccccccc", "cccccccccccc",
            "maestro/evolve/y", data_dir,
        )
        # A bare external-deferral phrase that is NOT itself a full
        # fetch-required narrative marker — so only the equal-SHA arm's own
        # has_fetch_truth check (not the downstream fetch floor's narrative
        # match) can catch it.
        text = "Resolved on branch, awaiting merge — PR #3208."
        outcome, verdict = gate_remediation_verdict(
            "task-eq-stale", text, "partial", data_dir, owned_awaiting_push=True,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fetch-missing] ")
        assert verdict.endswith(text)

    def test_owned_awaiting_push_default_flag_off_uses_fetch_floor(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Without the owned_awaiting_push signal (the default), the obligation-
        # fork floor never fires: an awaiting-tend-push verdict with no marker
        # falls through to the existing fetch floor ([fetch-missing]), preserving
        # the pre-existing behavior byte-for-byte.
        data_dir = Path("/fake/data")
        outcome, verdict = gate_remediation_verdict(
            "task-off",
            "Resolved on branch, awaiting tend push — PR #3208.",
            "success",
            data_dir,
        )
        assert outcome == "partial"
        assert verdict.startswith("[fetch-missing] ")

    def test_owned_awaiting_push_no_deferral_phrase_ignores_fork_floor(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The obligation-fork floor only guards external-deferral phrasing. An
        # owned AWAITING_PUSH lap whose verdict carries no such phrase (and no
        # fetch-required narrative) passes through untouched even with the flag.
        data_dir = Path("/fake/data")
        text = "Fixed the failing test and pushed the fix to the PR branch."
        outcome, verdict = gate_remediation_verdict(
            "task-np", text, "success", data_dir, owned_awaiting_push=True,
        )
        assert outcome == "success"
        assert verdict == text


class TestRecordObligationForkTruth:
    """Tests for the obligation-fork marker family."""

    def test_marker_persists_both_shas_and_is_idempotent(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Criterion 5: the marker persists both HEAD and origin/<branch> SHAs,
        # and a second call for the same PR/turn does not duplicate the row.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "task-a", 3208, "head-sha-1", "remote-sha-1",
            "maestro/evolve/z", data_dir, remote_is_ancestor=True,
        )
        fork = get_obligation_fork("task-a", data_dir)
        assert fork is not None
        assert fork.head_sha == "head-sha-1"
        assert fork.remote_sha == "remote-sha-1"
        assert fork.branch == "maestro/evolve/z"
        assert fork.remote_is_ancestor is True
        # head_ahead is the indicting fork: differing SHAs AND origin an ancestor
        # of HEAD (genuinely ahead) → a push is owed.
        assert fork.head_ahead is True

        # Idempotent per PR/turn: a second identical call appends no new row.
        raw = mem_fs["/fake/data/obligation_fork_markers.jsonl"]
        assert raw.count("\n") == 1
        record_obligation_fork_truth(
            "task-a", 3208, "head-sha-1", "remote-sha-1",
            "maestro/evolve/z", data_dir, remote_is_ancestor=True,
        )
        raw_after = mem_fs["/fake/data/obligation_fork_markers.jsonl"]
        assert raw_after.count("\n") == 1

    def test_equal_shas_report_not_ahead(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Equal HEAD and origin/<branch> SHAs mean the push is discharged.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "task-b", 42, "same-sha", "same-sha", "branch-b", data_dir,
        )
        fork = get_obligation_fork("task-b", data_dir)
        assert fork is not None
        assert fork.head_ahead is False

    def test_empty_task_id_not_written(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An empty task_id carries no key to match on, so nothing is written.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "", 1, "h", "r", "b", data_dir,
        )
        assert "/fake/data/obligation_fork_markers.jsonl" not in mem_fs
        assert get_obligation_fork("", data_dir) is None

    def test_get_returns_none_when_absent(
        self, mem_fs: dict[str, str],
    ) -> None:
        # No marker for the task → None (the gate then treats it fork-unread).
        assert get_obligation_fork("task-missing", Path("/fake/data")) is None

    def test_distinct_turn_same_pr_appends_new_row(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A different task (turn) on the same PR records its own row; the gate
        # reads the marker keyed to that turn.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "turn-1", 7, "h1", "r1", "br", data_dir,
        )
        record_obligation_fork_truth(
            "turn-2", 7, "h2", "r2", "br", data_dir,
        )
        raw = mem_fs["/fake/data/obligation_fork_markers.jsonl"]
        assert raw.count("\n") == 2
        assert get_obligation_fork("turn-1", data_dir).head_sha == "h1"
        assert get_obligation_fork("turn-2", data_dir).head_sha == "h2"

    def test_malformed_lines_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A corrupted line and a blank line are tolerated; a well-formed matching
        # row still reads.
        data_dir = Path("/fake/data")
        mem_fs["/fake/data/obligation_fork_markers.jsonl"] = (
            "not json\n"
            "\n"
            '{"task_id": "task-ok", "pr_number": 9, "head_sha": "h", '
            '"remote_sha": "r", "branch": "b"}\n'
        )
        fork = get_obligation_fork("task-ok", data_dir)
        assert fork is not None
        assert fork.branch == "b"

    def test_row_missing_string_field_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A row whose SHA fields are not all strings is ignored, so a partial
        # record never poses as a fork.
        data_dir = Path("/fake/data")
        mem_fs["/fake/data/obligation_fork_markers.jsonl"] = (
            '{"task_id": "task-partial", "pr_number": 9, "head_sha": 123, '
            '"remote_sha": "r", "branch": "b"}\n'
        )
        assert get_obligation_fork("task-partial", data_dir) is None

    def test_os_error_reading_returns_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        # An OSError reading the ledger fails closed to None (gate treats the
        # verdict as fork-unread), mirroring has_fetch_truth.
        data_dir = Path("/fake/data")
        record_obligation_fork_truth(
            "task-e", 1, "h", "r", "b", data_dir,
        )
        with patch("builtins.open", side_effect=OSError("disk error")):
            assert get_obligation_fork("task-e", data_dir) is None


class TestJsonlObligationForkLedger:
    """The default flag-off obligation-fork backend wrapping the JSONL core."""

    def test_record_delegates_to_writer_with_idempotency(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-of")
        ledger = JsonlObligationForkLedger(data_dir)
        ledger.record("t1", 1, "h1", "r1", "b1", remote_is_ancestor=True)
        # A second record for the same (task_id, pr_number) is a no-op — the
        # shared JSONL writer's per-pair idempotency guard is the one code path.
        ledger.record("t1", 1, "h1", "r1", "b1", remote_is_ancestor=True)
        raw = mem_fs[str(data_dir / "obligation_fork_markers.jsonl")]
        assert raw.count("\n") == 1
        assert ledger.get("t1") == ObligationFork(
            head_sha="h1", remote_sha="r1", branch="b1",
            remote_is_ancestor=True,
        )

    def test_record_drops_empty_task_id(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-of-guard")
        ledger = JsonlObligationForkLedger(data_dir)
        ledger.record("", 1, "h", "r", "b")
        # The shared writer's guard never persists a keyless marker.
        assert str(data_dir / "obligation_fork_markers.jsonl") not in mem_fs
        assert ledger.get("") is None

    def test_get_absent_returns_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonlObligationForkLedger(Path("/fake/jsonl-of-absent"))
        assert ledger.get("t1") is None


def test_load_all_obligation_forks_is_oldest_first(
    mem_fs: dict[str, str],
) -> None:
    """The SQLite seed source returns records in append (oldest-first) order."""
    data_dir = Path("/fake/of-loadall")
    record_obligation_fork_truth("t1", 1, "h1", "r1", "b1", data_dir)
    record_obligation_fork_truth(
        "t2", 2, "h2", "r2", "b2", data_dir, remote_is_ancestor=True,
    )
    assert load_all_obligation_forks(data_dir) == [
        ObligationForkRecord(
            task_id="t1", pr_number=1, head_sha="h1", remote_sha="r1",
            branch="b1", remote_is_ancestor=False,
        ),
        ObligationForkRecord(
            task_id="t2", pr_number=2, head_sha="h2", remote_sha="r2",
            branch="b2", remote_is_ancestor=True,
        ),
    ]


def test_load_all_obligation_forks_absent_file_is_empty() -> None:
    assert load_all_obligation_forks(Path("/fake/of-loadall-absent")) == []


def test_load_all_obligation_forks_skips_malformed_and_partial_rows(
    mem_fs: dict[str, str],
) -> None:
    """Malformed JSON and rows missing a required field are dropped.

    Mirrors :func:`get_obligation_fork`'s field coercion: a row whose string
    fields are not all strings is skipped, so the seed never carries a partial
    record the JSONL reader would have refused.
    """
    data_dir = Path("/fake/of-loadall-malformed")
    mem_fs[str(data_dir / "obligation_fork_markers.jsonl")] = "\n".join([
        "not-json",
        json.dumps({
            "task_id": "ok", "pr_number": 9, "head_sha": "h",
            "remote_sha": "r", "branch": "b",
        }),
        # branch is not a string → skipped, exactly like get_obligation_fork
        json.dumps({
            "task_id": "bad", "pr_number": 9, "head_sha": "h",
            "remote_sha": "r", "branch": 123,
        }),
    ]) + "\n"
    assert load_all_obligation_forks(data_dir) == [
        ObligationForkRecord(
            task_id="ok", pr_number=9, head_sha="h", remote_sha="r",
            branch="b", remote_is_ancestor=False,
        ),
    ]


def test_load_all_obligation_forks_drops_bad_pr_number_the_jsonl_reader_keeps(
    mem_fs: dict[str, str],
) -> None:
    """Pin the accepted seed asymmetry: a bad-``pr_number`` row diverges.

    :func:`get_obligation_fork` never inspects ``pr_number``, so a corrupt
    legacy row whose ``pr_number`` is missing (or non-``int``) but whose three
    string fields are valid is still returned by the JSONL reader pre-flip — yet
    :func:`load_all_obligation_forks` (the one-time SQLite seed source) requires
    an ``int`` ``pr_number`` for the store's key, so that row is dropped and
    vanishes post-flip. Only a hand-edited or corrupt file can reach this
    (the writer always emits an ``int`` ``pr_number``); this test locks the
    documented, accepted asymmetry so a later refactor can't silently change it.
    """
    data_dir = Path("/fake/of-loadall-bad-prnum")
    mem_fs[str(data_dir / "obligation_fork_markers.jsonl")] = "\n".join([
        # No pr_number key at all.
        json.dumps({
            "task_id": "missing", "head_sha": "h1",
            "remote_sha": "r1", "branch": "b1",
        }),
        # pr_number present but a string, not an int.
        json.dumps({
            "task_id": "nonint", "pr_number": "9", "head_sha": "h2",
            "remote_sha": "r2", "branch": "b2",
        }),
    ]) + "\n"
    # The JSONL reader still hands both rows back (it ignores pr_number).
    assert get_obligation_fork("missing", data_dir) == ObligationFork(
        head_sha="h1", remote_sha="r1", branch="b1", remote_is_ancestor=False,
    )
    assert get_obligation_fork("nonint", data_dir) == ObligationFork(
        head_sha="h2", remote_sha="r2", branch="b2", remote_is_ancestor=False,
    )
    # The SQLite seed source drops both — no int pr_number to key on.
    assert load_all_obligation_forks(data_dir) == []


def test_jsonl_and_sqlite_obligation_forks_agree(
    mem_fs: dict[str, str],
) -> None:
    """Both backends return the same ObligationFork for the same inputs.

    The proposal-106 substrate-parity contract for the obligation-fork ledger:
    a marker written through either backend reads back identically, so the
    gate's owned-``AWAITING_PUSH`` floor is invariant across
    ``[paths] sqlite_state_enabled``.
    """
    data_dir = Path("/fake/of-parity")
    jsonl = JsonlObligationForkLedger(data_dir)
    sqlite = SqliteObligationForkLedger(StateStore(":memory:"))
    markers = [
        ("t1", 1, "hA", "rA", "bA", True),    # genuinely ahead
        ("t2", 2, "same", "same", "bB", False),  # discharged (not ahead)
        ("t3", 3, "hC", "rC", "bC", False),   # diverged/behind
    ]
    for task_id, pr, head, remote, branch, ancestor in markers:
        jsonl.record(
            task_id, pr, head, remote, branch, remote_is_ancestor=ancestor,
        )
        sqlite.record(
            task_id, pr, head, remote, branch, remote_is_ancestor=ancestor,
        )
    for task_id in ("t1", "t2", "t3", "absent"):
        assert jsonl.get(task_id) == sqlite.get(task_id)
    # And the discriminating property matches on the indicting fork.
    jf = jsonl.get("t1")
    assert jf is not None
    assert jf.head_ahead is True


def test_jsonl_and_sqlite_obligation_forks_agree_via_legacy_seed(
    mem_fs: dict[str, str],
) -> None:
    """SQLite seeded from the JSONL agrees, incl. malformed + missing-ancestor.

    Seeds a legacy file carrying a malformed line and a legacy row missing
    ``remote_is_ancestor`` (both readers default it to ``False``), mirrors the
    valid rows into SQLite via the one-time seed source, and asserts each keyed
    read matches.
    """
    data_dir = Path("/fake/of-parity-seed")
    mem_fs[str(data_dir / "obligation_fork_markers.jsonl")] = "\n".join([
        # legacy row lacking remote_is_ancestor → defaults False on both sides
        json.dumps({
            "task_id": "t1", "pr_number": 1, "head_sha": "hA",
            "remote_sha": "hA", "branch": "bA",
        }),
        "not-json",  # malformed line skipped by both
        json.dumps({
            "task_id": "t2", "pr_number": 2, "head_sha": "hB",
            "remote_sha": "rB", "branch": "bB", "remote_is_ancestor": True,
        }),
    ]) + "\n"
    jsonl = JsonlObligationForkLedger(data_dir)
    sqlite = SqliteObligationForkLedger(
        StateStore(":memory:"), legacy_jsonl_dir=data_dir,
    )
    for task_id in ("t1", "t2", "absent"):
        assert jsonl.get(task_id) == sqlite.get(task_id)
    # The missing-ancestor legacy row defaults to not-ahead on both.
    jf = jsonl.get("t1")
    assert jf is not None
    assert jf.remote_is_ancestor is False


def test_gate_reads_supplied_obligation_fork_backend(
    mem_fs: dict[str, str],
) -> None:
    """The owned-AWAITING_PUSH floor reads the supplied backend, not data_dir.

    The SQLite backend carries a HEAD-genuinely-ahead marker while the JSONL
    ``data_dir`` has none. Routing the fork read through the backend yields the
    ``[push-owed]`` partial; had the gate read ``data_dir`` it would have found
    no marker and returned ``[fork-missing]`` — so the marker prefix proves the
    backend was the source.
    """
    backend = SqliteObligationForkLedger(StateStore(":memory:"))
    backend.record(
        "task-be", 3208, "aaaaaaaaaaaa", "bbbbbbbbbbbb",
        "maestro/evolve/x", remote_is_ancestor=True,
    )
    outcome, verdict = gate_remediation_verdict(
        "task-be",
        "Resolved on branch, awaiting tend push — PR #3208.",
        "partial",
        Path("/fake/empty-no-marker"),  # data_dir carries no fork marker
        owned_awaiting_push=True,
        obligation_fork=backend,
    )
    assert outcome == "partial"
    assert verdict.startswith("[push-owed] ")
    assert "origin/maestro/evolve/x" in verdict


def test_gate_none_backend_falls_back_to_data_dir(
    mem_fs: dict[str, str],
) -> None:
    """With no backend the gate reads the fork from data_dir — behaviour-identical.

    The ``None`` default preserves the pre-fold JSONL path, so callers that pass
    no backend (including the existing gate tests) stay green: here the marker
    lives only in the ``data_dir`` JSONL and the head-ahead fork still downgrades
    to ``[push-owed]``.
    """
    data_dir = Path("/fake/of-fallback")
    record_obligation_fork_truth(
        "task-fb", 3208, "aaaaaaaaaaaa", "bbbbbbbbbbbb",
        "maestro/evolve/x", data_dir, remote_is_ancestor=True,
    )
    outcome, verdict = gate_remediation_verdict(
        "task-fb",
        "Resolved on branch, awaiting tend push — PR #3208.",
        "partial",
        data_dir,
        owned_awaiting_push=True,
    )
    assert outcome == "partial"
    assert verdict.startswith("[push-owed] ")


class TestJsonlFetchTruthLedger:
    """The default flag-off fetch-truth backend wrapping the JSONL core."""

    def test_is_runtime_checkable_fetch_truth_backend(self) -> None:
        # ``@runtime_checkable`` lets the agent's backend-selection tests assert
        # structural conformance.
        assert isinstance(
            JsonlFetchTruthLedger(Path("/fake/rc")), FetchTruthBackend,
        )

    def test_record_delegates_appends_per_call(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-ft")
        ledger = JsonlFetchTruthLedger(data_dir)
        ledger.record("t1", 1, False, head_sha="h1")
        # A second record for the same (task_id, pr_number) is a distinct row —
        # the fetch-truth writer appends per call with no dedupe.
        ledger.record("t1", 1, False, head_sha="h1")
        raw = mem_fs[str(data_dir / "fetch_truth_markers.jsonl")]
        assert raw.count("\n") == 2
        assert ledger.has("t1") is True

    def test_record_drops_empty_task_id(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-ft-guard")
        ledger = JsonlFetchTruthLedger(data_dir)
        ledger.record("", 1, True, head_sha="h")
        # The shared writer's guard never persists a keyless marker.
        assert str(data_dir / "fetch_truth_markers.jsonl") not in mem_fs
        assert ledger.has("") is False

    def test_has_absent_returns_false(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonlFetchTruthLedger(Path("/fake/jsonl-ft-absent"))
        assert ledger.has("t1") is False

    def test_relap_delegates(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/jsonl-ft-relap")
        ledger = JsonlFetchTruthLedger(data_dir)
        # An unlanded marker with a resolved head diverts; a landed one does not.
        ledger.record("t1", 5, False, head_sha="sha-x")
        ledger.record("t2", 6, True, head_sha="sha-y")
        assert ledger.relap(5, "sha-x") is True
        assert ledger.relap(6, "sha-y") is False
        assert ledger.relap(5, "other") is False


def test_load_all_fetch_truth_markers_is_oldest_first(
    mem_fs: dict[str, str],
) -> None:
    """The SQLite seed source returns records in append (oldest-first) order."""
    data_dir = Path("/fake/ft-loadall")
    record_fetch_truth("t1", 1, False, data_dir, head_sha="h1")
    record_fetch_truth("t2", 2, True, data_dir, head_sha="h2")
    assert load_all_fetch_truth_markers(data_dir) == [
        FetchTruthRecord(
            task_id="t1", pr_number=1, is_ancestor=False, head_sha="h1",
        ),
        FetchTruthRecord(
            task_id="t2", pr_number=2, is_ancestor=True, head_sha="h2",
        ),
    ]


def test_load_all_fetch_truth_markers_absent_file_is_empty() -> None:
    assert load_all_fetch_truth_markers(Path("/fake/ft-loadall-absent")) == []


def test_load_all_fetch_truth_markers_skips_malformed_and_partial_rows(
    mem_fs: dict[str, str],
) -> None:
    """Malformed JSON and rows missing a required field are dropped.

    A legacy three-field row (no ``head_sha``) is kept with the ``""`` default;
    a row whose ``is_ancestor`` is absent or non-bool, or whose ``pr_number`` is
    non-``int``, is skipped so the seed never carries a partial record.
    """
    data_dir = Path("/fake/ft-loadall-malformed")
    mem_fs[str(data_dir / "fetch_truth_markers.jsonl")] = "\n".join([
        "not-json",
        json.dumps({
            "task_id": "ok", "pr_number": 9, "is_ancestor": True,
            "head_sha": "h",
        }),
        # legacy three-field row → head_sha defaults to ""
        json.dumps({"task_id": "legacy", "pr_number": 4, "is_ancestor": False}),
        # is_ancestor missing → skipped
        json.dumps({"task_id": "no-anc", "pr_number": 5}),
        # is_ancestor not a bool → skipped
        json.dumps({"task_id": "str-anc", "pr_number": 6, "is_ancestor": "no"}),
        # pr_number not an int → skipped
        json.dumps({"task_id": "bad-pr", "pr_number": "7", "is_ancestor": True}),
    ]) + "\n"
    assert load_all_fetch_truth_markers(data_dir) == [
        FetchTruthRecord(
            task_id="ok", pr_number=9, is_ancestor=True, head_sha="h",
        ),
        FetchTruthRecord(
            task_id="legacy", pr_number=4, is_ancestor=False, head_sha="",
        ),
    ]


def test_jsonl_and_sqlite_fetch_truth_agree(
    mem_fs: dict[str, str],
) -> None:
    """Both backends answer ``has`` / ``relap`` identically for the same inputs.

    The proposal-106 substrate-parity contract for the fetch-truth ledger: a
    marker written through either backend reads back identically, so the gate's
    fetch floor is invariant across ``[paths] sqlite_state_enabled``.
    """
    data_dir = Path("/fake/ft-parity")
    jsonl = JsonlFetchTruthLedger(data_dir)
    sqlite = SqliteFetchTruthLedger(StateStore(":memory:"))
    markers = [
        ("t1", 1, False, "sha-unlanded"),
        ("t2", 2, True, "sha-landed"),
        ("t3", 3, False, ""),  # legacy no-head shape
    ]
    for task_id, pr, anc, head in markers:
        jsonl.record(task_id, pr, anc, head_sha=head)
        sqlite.record(task_id, pr, anc, head_sha=head)
    for task_id in ("t1", "t2", "t3", "absent", ""):
        assert jsonl.has(task_id) == sqlite.has(task_id)
    for pr, head in [
        (1, "sha-unlanded"),  # unlanded, head matches → divert
        (2, "sha-landed"),    # landed → no divert
        (3, ""),              # empty head → no divert
        (3, "x"),             # empty-stored head never matches a query
        (1, "other"),         # mismatched head
        (99, "sha-unlanded"),  # mismatched pr
    ]:
        assert jsonl.relap(pr, head) == sqlite.relap(pr, head)


def test_jsonl_and_sqlite_fetch_truth_agree_via_legacy_seed(
    mem_fs: dict[str, str],
) -> None:
    """SQLite seeded from the JSONL agrees, incl. malformed + legacy no-head."""
    data_dir = Path("/fake/ft-parity-seed")
    mem_fs[str(data_dir / "fetch_truth_markers.jsonl")] = "\n".join([
        json.dumps({
            "task_id": "t1", "pr_number": 1, "is_ancestor": False,
            "head_sha": "sha-x",
        }),
        "not-json",  # malformed line skipped by both
        json.dumps({
            "task_id": "t2", "pr_number": 2, "is_ancestor": True,
            "head_sha": "sha-y",
        }),
        # legacy row lacking head_sha → head defaults "" on both sides
        json.dumps({"task_id": "t3", "pr_number": 3, "is_ancestor": False}),
    ]) + "\n"
    jsonl = JsonlFetchTruthLedger(data_dir)
    sqlite = SqliteFetchTruthLedger(
        StateStore(":memory:"), legacy_jsonl_dir=data_dir,
    )
    for task_id in ("t1", "t2", "t3", "absent"):
        assert jsonl.has(task_id) == sqlite.has(task_id)
    for pr, head in [(1, "sha-x"), (2, "sha-y"), (3, "sha-x"), (3, "")]:
        assert jsonl.relap(pr, head) == sqlite.relap(pr, head)


def test_gate_reads_supplied_fetch_truth_backend(
    mem_fs: dict[str, str],
) -> None:
    """The plain fetch floor reads the supplied backend, not ``data_dir``.

    The marker lives only in the backend while the ``data_dir`` JSONL has none;
    routing the fetch read through the backend lets the verdict stand. Had the
    gate read ``data_dir`` it would have downgraded to ``[fetch-missing]``.
    """
    backend = SqliteFetchTruthLedger(StateStore(":memory:"))
    backend.record("task-ft", 2647, True)
    text = "Resolved on branch, awaiting tend push — PR #2647 not yet landed."
    outcome, verdict = gate_remediation_verdict(
        "task-ft",
        text,
        "success",
        Path("/fake/empty-no-marker"),  # data_dir carries no fetch marker
        fetch_truth=backend,
    )
    assert outcome == "success"
    assert verdict == text


def test_gate_fetch_truth_none_backend_falls_back_to_data_dir(
    mem_fs: dict[str, str],
) -> None:
    """With no backend the gate reads the fetch marker from ``data_dir``."""
    data_dir = Path("/fake/ft-fallback")
    record_fetch_truth("task-fb", 2647, True, data_dir)
    text = "Resolved on branch, awaiting tend push — PR #2647."
    outcome, verdict = gate_remediation_verdict(
        "task-fb", text, "success", data_dir,
    )
    assert outcome == "success"
    assert verdict == text


def test_gate_fetch_floor_reads_empty_backend_over_data_dir(
    mem_fs: dict[str, str],
) -> None:
    """The marker is in ``data_dir`` but the supplied backend is empty.

    Routing through the empty backend downgrades, proving the backend — not
    ``data_dir`` — is the fetch source.
    """
    data_dir = Path("/fake/ft-discriminate")
    record_fetch_truth("task-d", 2647, True, data_dir)
    empty_backend = SqliteFetchTruthLedger(StateStore(":memory:"))
    outcome, verdict = gate_remediation_verdict(
        "task-d",
        "Resolved on branch, awaiting tend push — PR #2647.",
        "success",
        data_dir,
        fetch_truth=empty_backend,
    )
    assert outcome == "partial"
    assert verdict.startswith("[fetch-missing] ")


def test_gate_obligation_arm_reads_supplied_fetch_truth_backend(
    mem_fs: dict[str, str],
) -> None:
    """The owned-AWAITING_PUSH discharge arm reads the supplied fetch backend.

    The fork shows HEAD == origin/<branch> (discharged, not ahead), so the arm
    falls to its freshness check. The fetch-truth marker lives only in the
    supplied backend while ``data_dir`` has none → the external deferral stands,
    proving the arm's fetch read routed through the backend.
    """
    fork_backend = SqliteObligationForkLedger(StateStore(":memory:"))
    fork_backend.record(
        "task-arm", 3208, "cccccccccccc", "cccccccccccc", "maestro/evolve/z",
    )
    truth_backend = SqliteFetchTruthLedger(StateStore(":memory:"))
    truth_backend.record("task-arm", 3208, False)
    text = "Delivered to PR, awaiting review/merge — PR #3208."
    outcome, verdict = gate_remediation_verdict(
        "task-arm",
        text,
        "partial",
        Path("/fake/empty-no-marker"),  # data_dir carries no fetch marker
        owned_awaiting_push=True,
        obligation_fork=fork_backend,
        fetch_truth=truth_backend,
    )
    assert outcome == "partial"
    assert verdict == text


def test_gate_obligation_arm_empty_fetch_truth_backend_downgrades(
    mem_fs: dict[str, str],
) -> None:
    """Same discharge arm, but the fetch marker is in ``data_dir`` only.

    The supplied backend is empty → the arm downgrades to ``[fetch-missing]``,
    proving it reads the backend rather than ``data_dir``.
    """
    data_dir = Path("/fake/ft-arm-discriminate")
    record_fetch_truth("task-arm2", 3208, False, data_dir)
    fork_backend = SqliteObligationForkLedger(StateStore(":memory:"))
    fork_backend.record(
        "task-arm2", 3208, "cccccccccccc", "cccccccccccc", "maestro/evolve/z",
    )
    empty_truth = SqliteFetchTruthLedger(StateStore(":memory:"))
    outcome, verdict = gate_remediation_verdict(
        "task-arm2",
        "Resolved on branch, awaiting merge — PR #3208.",
        "partial",
        data_dir,
        owned_awaiting_push=True,
        obligation_fork=fork_backend,
        fetch_truth=empty_truth,
    )
    assert outcome == "partial"
    assert verdict.startswith("[fetch-missing] ")


class TestCoerceVerdictToSettledOnMerge:
    """Record-time merge-truth binding: phantom not-landed unwriteable on merge."""

    def test_merged_pr_coerces_phantom_not_landed_to_settled(self) -> None:
        # (a) A merged PR (is_pr_merged -> True) records a settled verdict even
        # when the caller passes a not-landed / awaiting-tend-push stamp.
        outcome, verdict = coerce_verdict_to_settled_on_merge(
            2809,
            "Resolved on branch, awaiting tend push — PR #2809 not-landed.",
            "partial",
            is_pr_merged=lambda _pr: True,
        )
        assert outcome == "success"
        assert "not-landed" not in verdict
        assert "awaiting tend push" not in verdict
        assert "#2809" in verdict

    def test_unlanded_pr_records_verdict_unchanged(self) -> None:
        # (b) A genuinely-unlanded PR (is_pr_merged -> False) records the
        # not-landed verdict unchanged from the caller-supplied outcome.
        text = "Resolved on branch, awaiting tend push — PR #2810 not-landed."
        outcome, verdict = coerce_verdict_to_settled_on_merge(
            2810,
            text,
            "partial",
            is_pr_merged=lambda _pr: False,
        )
        assert outcome == "partial"
        assert verdict == text

    def test_merged_state_derived_solely_via_chokepoint(self) -> None:
        # (c) The coercion derives merged-state solely through the injected
        # is_pr_settled chokepoint predicate, never from a caller-supplied stamp:
        # a verdict whose text *claims* merged is still left unchanged when the
        # chokepoint reports the PR not merged, and the predicate is consulted
        # with exactly the PR number.
        consulted: list[int] = []

        def is_pr_merged(pr_number: int) -> bool:
            consulted.append(pr_number)
            return False

        claims_merged = "PR #2811 merged into main — fully resolved."
        outcome, verdict = coerce_verdict_to_settled_on_merge(
            2811,
            claims_merged,
            "success",
            is_pr_merged=is_pr_merged,
        )
        # The stamp lies (text claims merged) but the chokepoint is authoritative.
        assert consulted == [2811]
        assert outcome == "success"
        assert verdict == claims_merged
