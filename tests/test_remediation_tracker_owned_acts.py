"""Owned-acts report, preflight/tracker stores, readiness snapshots and owed-act
divergence; split out of ``tests/test_remediation_tracker.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro import remediation_tracker
from maestro.audit_checks import (
    GateActivation,
    format_known_state_summary,
)
from maestro.audit_checks_phantom import (
    _persist_handoff_stand_down_row,
)
from maestro.deed_ledger import DeedType, JsonlDeedLedger, record_deed
from maestro.remediation_tracker import (
    _OWED_ACT_DIVERGENCE_DATA_FLOOR,
    _OWED_ACT_DIVERGENCE_WINDOW,
    COOLDOWN_SKIP_GATE,
    JsonlStandDownVerdictLedger,
    JsonPreflightPassLedger,
    JsonRemediationTrackerStore,
    OwedActDivergence,
    OwnedAct,
    PreflightPassBackend,
    PreflightPassRecord,
    RemediationTracker,
    RemediationTrackerBackend,
    TrackerState,
    _parse_tracker_payload,
    classify_remediation_outcome_state,
    find_stand_down,
    format_cooldown_skip_detail,
    format_owed_act_divergence,
    format_owned_acts_report,
    has_live_stand_down,
    latest_pr_readiness_snapshots,
    load_all_preflight_passes,
    load_tracker_state,
    measure_forced_vs_chosen_quiet,
    measure_owed_act_divergence,
    measure_standdown_production,
    read_chosen_stand_down_acts,
    read_handoff_deed_acts,
    read_obsolete_plan_acts,
    record_preflight_pass,
    record_stand_down,
    release_stand_downs,
)
from maestro.state_store import (
    SqlitePreflightPassStore,
    SqliteRemediationTrackerStore,
    SqliteStandDownVerdictLedger,
    StateStore,
)
from tests.test_remediation_tracker import (
    _mb_task,
)

# ──────────────────────────────────────────────────────────────────────
# owned-acts — outward-facing surface over chosen terminal dispositions
# ──────────────────────────────────────────────────────────────────────


def _seed_stand_downs(
    mem_fs: dict[str, str], data_dir: Path, lines: list[dict[str, object]],
) -> None:
    """Write ``stand_down_verdicts.jsonl`` verbatim rows via ``mem_fs``."""
    body = "\n".join(json.dumps(line) for line in lines)
    mem_fs[str(data_dir / "stand_down_verdicts.jsonl")] = (
        body + ("\n" if body else "")
    )


def _seed_obsolete_plans(
    mem_fs: dict[str, str], data_dir: Path, rows: list[dict[str, object]],
) -> None:
    """Write ``evolution_plans.jsonl`` verbatim rows via ``mem_fs``."""
    body = "\n".join(json.dumps(row) for row in rows)
    mem_fs[str(data_dir / "evolution_plans.jsonl")] = (
        body + ("\n" if body else "")
    )


class TestReadChosenStandDownActs:
    """Additive full-row reader over ``stand_down_verdicts.jsonl``."""

    def test_missing_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert read_chosen_stand_down_acts(JsonlStandDownVerdictLedger(Path("/fake/data"))) == []

    def test_oserror_returns_empty(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_stand_downs(
            mem_fs, data_dir,
            [{"pr": 5, "head_sha": "s", "recorded_at": "2026-07-01T00:00:00"}],
        )
        with patch("builtins.open", side_effect=OSError("boom")):
            assert read_chosen_stand_down_acts(JsonlStandDownVerdictLedger(data_dir)) == []

    def test_ahead_true_is_chosen(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_stand_downs(mem_fs, data_dir, [{
            "pr": 3179, "head_sha": "abc",
            "recorded_at": "2026-07-01T00:00:00+00:00",
            "branch_ahead_of_origin": True,
        }])
        acts = read_chosen_stand_down_acts(JsonlStandDownVerdictLedger(data_dir))
        assert acts == [OwnedAct(
            kind="stand_down",
            timestamp="2026-07-01T00:00:00+00:00",
            lineage="#3179",
            chosen=True,
        )]

    def test_ahead_false_or_missing_is_forced(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_stand_downs(mem_fs, data_dir, [
            {"pr": 1, "head_sha": "a", "recorded_at": "2026-07-01T00:00:00",
             "branch_ahead_of_origin": False},
            {"pr": 2, "head_sha": "b", "recorded_at": "2026-07-02T00:00:00"},
        ])
        acts = read_chosen_stand_down_acts(JsonlStandDownVerdictLedger(data_dir))
        assert [a.chosen for a in acts] == [False, False]
        assert [a.lineage for a in acts] == ["#1", "#2"]

    def test_skips_release_and_corrupt_rows(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        # A release row (no head_sha), a bool pr, a non-positive pr, and a
        # non-dict list are all skipped; only the genuine verdict survives.
        mem_fs[str(data_dir / "stand_down_verdicts.jsonl")] = "\n".join([
            "",
            "{not valid json",
            json.dumps([1, 2]),
            json.dumps({"pr": 7, "released_at": "2026-07-01T00:00:00"}),
            json.dumps({"pr": True, "head_sha": "x", "recorded_at": "t"}),
            json.dumps({"pr": 0, "head_sha": "x", "recorded_at": "t"}),
            json.dumps({"pr": 9, "head_sha": 5, "recorded_at": "t"}),
            json.dumps({
                "pr": 9, "head_sha": "s", "recorded_at": "2026-07-03T00:00:00",
            }),
        ]) + "\n"
        acts = read_chosen_stand_down_acts(JsonlStandDownVerdictLedger(data_dir))
        assert len(acts) == 1
        assert acts[0].lineage == "#9"


class TestReadObsoletePlanActs:
    """Additive full-row reader over ``evolution_plans.jsonl``."""

    def test_missing_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert read_obsolete_plan_acts(Path("/fake/data")) == []

    def test_oserror_returns_empty(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_obsolete_plans(
            mem_fs, data_dir,
            [{"plan_id": "p", "status": "obsolete", "created_at": "t"}],
        )
        with patch("builtins.open", side_effect=OSError("boom")):
            assert read_obsolete_plan_acts(data_dir) == []

    def test_obsolete_plan_is_always_chosen(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_obsolete_plans(mem_fs, data_dir, [{
            "plan_id": "abc123def456", "status": "obsolete",
            "reviewed_at": "2026-07-05T00:00:00+00:00",
            "created_at": "2026-07-01T00:00:00+00:00",
        }])
        acts = read_obsolete_plan_acts(data_dir)
        assert acts == [OwnedAct(
            kind="obsolete_prune",
            timestamp="2026-07-05T00:00:00+00:00",
            lineage="abc123def456",
            chosen=True,
        )]

    def test_door_refused_mint_is_not_an_owned_prune(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A mint the submission door refused is skipped; a prune is kept (#4157)."""
        data_dir = Path("/fake/data")
        _seed_obsolete_plans(mem_fs, data_dir, [
            {"plan_id": "minted", "status": "obsolete", "reviewed_at": "t",
             "refused_at_submission": True},
            {"plan_id": "pruned", "status": "obsolete", "reviewed_at": "t"},
        ])
        acts = read_obsolete_plan_acts(data_dir)
        assert [a.lineage for a in acts] == ["pruned"]

    def test_created_at_fallback_and_pr_url_lineage(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        _seed_obsolete_plans(mem_fs, data_dir, [{
            "status": "obsolete",
            "created_at": "2026-07-02T00:00:00+00:00",
            "pr_url": "https://github.com/o/r/pull/3211",
        }])
        acts = read_obsolete_plan_acts(data_dir)
        assert acts[0].timestamp == "2026-07-02T00:00:00+00:00"
        assert acts[0].lineage == "#3211"

    def test_no_plan_id_no_pr_url_gives_empty_lineage(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        # Non-string reviewed_at → empty timestamp; non-string pr_url and no
        # plan_id → empty lineage (the "-" text fallback exercises here).
        _seed_obsolete_plans(mem_fs, data_dir, [
            {"status": "obsolete", "reviewed_at": 5, "pr_url": 42},
        ])
        acts = read_obsolete_plan_acts(data_dir)
        assert acts == [OwnedAct(
            kind="obsolete_prune", timestamp="", lineage="", chosen=True,
        )]

    def test_skips_non_obsolete_and_corrupt_rows(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        mem_fs[str(data_dir / "evolution_plans.jsonl")] = "\n".join([
            "",
            "{not valid json",
            json.dumps(["not", "a", "dict"]),
            json.dumps({"plan_id": "live1", "status": "approved"}),
            json.dumps({
                "plan_id": "gone1", "status": "obsolete", "created_at": "t",
            }),
        ]) + "\n"
        acts = read_obsolete_plan_acts(data_dir)
        assert [a.lineage for a in acts] == ["gone1"]


class TestReadHandoffDeedActs:
    """Additive full-row reader over ``deed_ledger.jsonl`` HANDOFF_DELIVERED deeds."""

    def test_empty_ledger_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert read_handoff_deed_acts(JsonlDeedLedger(Path("/fake/data"))) == []

    def test_one_row_per_handoff_deed_with_pr_lineage(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4001, "handed off",
            now="2026-07-01T00:00:00+00:00",
        )
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4002, "handed off",
            now="2026-07-02T00:00:00+00:00",
        )
        acts = read_handoff_deed_acts(JsonlDeedLedger(data_dir))
        assert len(acts) == 2
        assert {a.lineage for a in acts} == {"#4001", "#4002"}
        assert all(a.kind == "handoff" for a in acts)

    def test_every_handoff_row_is_forced(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4003, "handed off",
            now="2026-07-03T00:00:00+00:00",
        )
        acts = read_handoff_deed_acts(JsonlDeedLedger(data_dir))
        assert acts == [OwnedAct(
            kind="handoff",
            timestamp="2026-07-03T00:00:00+00:00",
            lineage="#4003",
            chosen=False,
        )]

    def test_only_handoff_deeds_are_projected(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        record_deed(
            data_dir, DeedType.PR_CLOSE, 5001, "closed",
            now="2026-07-04T00:00:00+00:00",
        )
        record_deed(
            data_dir, DeedType.MERGE, 5002, "merged",
            now="2026-07-05T00:00:00+00:00",
        )
        record_deed(
            data_dir, DeedType.OWNED_PRUNE, None, "pruned",
            now="2026-07-06T00:00:00+00:00",
        )
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 5003, "handed off",
            now="2026-07-07T00:00:00+00:00",
        )
        acts = read_handoff_deed_acts(JsonlDeedLedger(data_dir))
        assert acts == [OwnedAct(
            kind="handoff",
            timestamp="2026-07-07T00:00:00+00:00",
            lineage="#5003",
            chosen=False,
        )]

    def test_handoff_deed_without_pr_gets_empty_lineage(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, None, "handed off",
            now="2026-07-08T00:00:00+00:00",
        )
        acts = read_handoff_deed_acts(JsonlDeedLedger(data_dir))
        assert acts == [OwnedAct(
            kind="handoff",
            timestamp="2026-07-08T00:00:00+00:00",
            lineage="",
            chosen=False,
        )]


class TestFormatOwnedActsReport:
    """The ``owned-acts`` render — text and JSON, populated and empty."""

    def _seed_both(self, mem_fs: dict[str, str], data_dir: Path) -> None:
        _seed_stand_downs(mem_fs, data_dir, [{
            "pr": 3179, "head_sha": "abc",
            "recorded_at": "2026-07-03T00:00:00+00:00",
            "branch_ahead_of_origin": True,
        }])
        _seed_obsolete_plans(mem_fs, data_dir, [{
            "plan_id": "abc123def456", "status": "obsolete",
            "reviewed_at": "2026-07-01T00:00:00+00:00",
        }])

    def test_handoff_rows_merge_in_without_perturbing_others(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Criteria 1-3 — one forced handoff row per deed, others unchanged."""
        data_dir = Path("/fake/data")
        self._seed_both(mem_fs, data_dir)
        # Two handoff deeds, dated to bracket the two existing rows.
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4242, "handed off",
            now="2026-07-02T00:00:00+00:00",
        )
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4243, "handed off",
            now="2026-07-04T00:00:00+00:00",
        )
        # The projection reads only handoff deeds — a non-handoff deed present
        # in the same ledger contributes no row.
        record_deed(
            data_dir, DeedType.PR_CLOSE, 9999, "closed",
            now="2026-07-05T00:00:00+00:00",
        )
        out = format_owned_acts_report(
            data_dir, 0, as_json=True,
            stand_down_backend=JsonlStandDownVerdictLedger(data_dir),
            deed_backend=JsonlDeedLedger(data_dir),
        )
        data = json.loads(out)
        # Criterion 1: exactly one row per HANDOFF_DELIVERED deed, carrying
        # that deed's pr_number as lineage.
        handoffs = [r for r in data if r["kind"] == "handoff"]
        assert [r["lineage"] for r in handoffs] == ["#4242", "#4243"]
        # Criterion 2: every handoff row is forced.
        assert all(r["chosen"] is False for r in handoffs)
        # Criterion 3: the pre-existing rows are unchanged in count/fields.
        obsolete = [r for r in data if r["kind"] == "obsolete_prune"]
        stand_down = [r for r in data if r["kind"] == "stand_down"]
        assert obsolete == [{
            "kind": "obsolete_prune",
            "timestamp": "2026-07-01T00:00:00+00:00",
            "lineage": "abc123def456",
            "chosen": True,
        }]
        assert stand_down == [{
            "kind": "stand_down",
            "timestamp": "2026-07-03T00:00:00+00:00",
            "lineage": "#3179",
            "chosen": True,
        }]
        # Full chronological merge: obsolete → handoff → stand_down → handoff.
        assert [r["kind"] for r in data] == [
            "obsolete_prune", "handoff", "stand_down", "handoff",
        ]

    def test_handoff_backfill_row_reported_once_as_handoff(
        self, mem_fs: dict[str, str],
    ) -> None:
        """#4319 — the production handoff shape (backfill + deed) lists once."""
        data_dir = Path("/fake/data")
        self._seed_both(mem_fs, data_dir)
        backend = JsonlStandDownVerdictLedger(data_dir)
        _persist_handoff_stand_down_row(
            4242, backend, now="2026-07-02T00:00:00+00:00",
        )
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4242, "handed off",
            now="2026-07-02T00:00:01+00:00",
        )
        out = format_owned_acts_report(
            data_dir, 0, as_json=True,
            stand_down_backend=backend,
            deed_backend=JsonlDeedLedger(data_dir),
        )
        data = json.loads(out)
        handoff_rows = [r for r in data if r["lineage"] == "#4242"]
        assert len(handoff_rows) == 1
        assert handoff_rows[0]["kind"] == "handoff"
        genuine = [r for r in data if r["lineage"] == "#3179"]
        assert genuine == [{
            "kind": "stand_down",
            "timestamp": "2026-07-03T00:00:00+00:00",
            "lineage": "#3179",
            "chosen": True,
        }]

    def test_handoff_backfill_without_deed_still_reported(
        self, mem_fs: dict[str, str],
    ) -> None:
        """#4319 guard — a backfill row with no deed (crash) is not lost."""
        data_dir = Path("/fake/data")
        backend = JsonlStandDownVerdictLedger(data_dir)
        _persist_handoff_stand_down_row(
            4242, backend, now="2026-07-02T00:00:00+00:00",
        )
        # A handoff deed for a *different* PR must not suppress this row.
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4243, "handed off",
            now="2026-07-02T00:00:01+00:00",
        )
        out = format_owned_acts_report(
            data_dir, 0, as_json=True,
            stand_down_backend=backend,
            deed_backend=JsonlDeedLedger(data_dir),
        )
        data = json.loads(out)
        assert [(r["kind"], r["lineage"]) for r in data] == [
            ("stand_down", "#4242"), ("handoff", "#4243"),
        ]

    def test_orphaned_backfill_kept_when_pr_refired(
        self, mem_fs: dict[str, str],
    ) -> None:
        """#4319 guard — one backfill collapses per deed, not every backfill."""
        data_dir = Path("/fake/data")
        backend = JsonlStandDownVerdictLedger(data_dir)
        # Crashed fire: backfill, no deed.
        _persist_handoff_stand_down_row(
            4242, backend, now="2026-07-01T00:00:00+00:00",
        )
        # Later re-fire for the same PR: backfill + deed.
        _persist_handoff_stand_down_row(
            4242, backend, now="2026-07-02T00:00:00+00:00",
        )
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4242, "handed off",
            now="2026-07-02T00:00:01+00:00",
        )
        out = format_owned_acts_report(
            data_dir, 0, as_json=True,
            stand_down_backend=backend,
            deed_backend=JsonlDeedLedger(data_dir),
        )
        data = json.loads(out)
        assert [r["kind"] for r in data] == ["stand_down", "handoff"]

    def test_same_pr_verdict_with_head_kept_beside_handoff(
        self, mem_fs: dict[str, str],
    ) -> None:
        """#4319 guard — only the empty-head backfill collapses, not a real verdict."""
        data_dir = Path("/fake/data")
        _seed_stand_downs(mem_fs, data_dir, [{
            "pr": 4242, "head_sha": "abc",
            "recorded_at": "2026-07-01T00:00:00+00:00",
        }])
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4242, "handed off",
            now="2026-07-02T00:00:00+00:00",
        )
        out = format_owned_acts_report(
            data_dir, 0, as_json=True,
            stand_down_backend=JsonlStandDownVerdictLedger(data_dir),
            deed_backend=JsonlDeedLedger(data_dir),
        )
        data = json.loads(out)
        assert [r["kind"] for r in data] == ["stand_down", "handoff"]

    def test_pre_existing_rows_unchanged_when_no_deeds(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Criterion 3 — an empty deed ledger leaves the report identical."""
        data_dir = Path("/fake/data")
        self._seed_both(mem_fs, data_dir)
        out = format_owned_acts_report(
            data_dir, 0, as_json=True,
            stand_down_backend=JsonlStandDownVerdictLedger(data_dir),
            deed_backend=JsonlDeedLedger(data_dir),
        )
        data = json.loads(out)
        assert [r["kind"] for r in data] == ["obsolete_prune", "stand_down"]

    def test_handoff_forced_indicator_in_text(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Criterion 2 — text mode marks the handoff row forced."""
        data_dir = Path("/fake/data")
        record_deed(
            data_dir, DeedType.HANDOFF_DELIVERED, 4242, "handed off",
            now="2026-07-02T00:00:00+00:00",
        )
        out = format_owned_acts_report(
            data_dir, 0, as_json=False,
            stand_down_backend=JsonlStandDownVerdictLedger(data_dir),
            deed_backend=JsonlDeedLedger(data_dir),
        )
        assert "handoff #4242 (forced)" in out

    def test_populated_text_shows_all_fields(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/data")
        self._seed_both(mem_fs, data_dir)
        out = format_owned_acts_report(data_dir, 0, as_json=False, stand_down_backend=JsonlStandDownVerdictLedger(data_dir), deed_backend=JsonlDeedLedger(data_dir))
        # Chronological order (obsolete 07-01 before stand-down 07-03).
        assert out.index("obsolete_prune") < out.index("stand_down")
        # The chosen-vs-forced distinction is visible in text mode.
        assert "(chosen)" in out
        assert "abc123def456" in out
        assert "#3179" in out
        assert "2026-07-03T00:00:00+00:00" in out

    def test_forced_indicator_visible(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_stand_downs(mem_fs, data_dir, [{
            "pr": 4, "head_sha": "z", "recorded_at": "2026-07-04T00:00:00",
        }])
        out = format_owned_acts_report(data_dir, 0, as_json=False, stand_down_backend=JsonlStandDownVerdictLedger(data_dir), deed_backend=JsonlDeedLedger(data_dir))
        assert "(forced)" in out

    def test_empty_text_line(self, mem_fs: dict[str, str]) -> None:
        out = format_owned_acts_report(Path("/fake/data"), 0, as_json=False, stand_down_backend=JsonlStandDownVerdictLedger(Path("/fake/data")), deed_backend=JsonlDeedLedger(Path("/fake/data")))
        assert "no owned acts recorded" in out

    def test_empty_json_is_bracket_pair(self, mem_fs: dict[str, str]) -> None:
        out = format_owned_acts_report(Path("/fake/data"), 0, as_json=True, stand_down_backend=JsonlStandDownVerdictLedger(Path("/fake/data")), deed_backend=JsonlDeedLedger(Path("/fake/data")))
        assert json.loads(out) == []
        assert out == "[]"

    def test_json_round_trip_fields(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        self._seed_both(mem_fs, data_dir)
        out = format_owned_acts_report(data_dir, 0, as_json=True, stand_down_backend=JsonlStandDownVerdictLedger(data_dir), deed_backend=JsonlDeedLedger(data_dir))
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 2
        for obj in data:
            assert set(obj) == {"kind", "timestamp", "lineage", "chosen"}
        assert data[0] == {
            "kind": "obsolete_prune",
            "timestamp": "2026-07-01T00:00:00+00:00",
            "lineage": "abc123def456",
            "chosen": True,
        }
        assert data[1]["kind"] == "stand_down"
        assert data[1]["chosen"] is True

    def test_n_keeps_most_recent(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        self._seed_both(mem_fs, data_dir)
        out = format_owned_acts_report(data_dir, 1, as_json=True, stand_down_backend=JsonlStandDownVerdictLedger(data_dir), deed_backend=JsonlDeedLedger(data_dir))
        data = json.loads(out)
        # Only the most-recent act (the 07-03 stand-down) survives the tail.
        assert len(data) == 1
        assert data[0]["kind"] == "stand_down"

    def test_empty_lineage_renders_dash(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/data")
        _seed_obsolete_plans(mem_fs, data_dir, [
            {"status": "obsolete", "created_at": "2026-07-01T00:00:00"},
        ])
        out = format_owned_acts_report(data_dir, 0, as_json=False, stand_down_backend=JsonlStandDownVerdictLedger(data_dir), deed_backend=JsonlDeedLedger(data_dir))
        assert "obsolete_prune - (chosen)" in out


def test_owned_acts_report_leaves_drought_measure_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 4 — the additive readers do not perturb rule 18's measure.

    ``measure_owned_act_drought`` reads the timestamp-only readers; the new
    report reads additive full-row readers over the SAME files. Rendering the
    report must leave both the drought count and the on-disk fixture
    byte-for-byte unchanged.
    """
    from maestro.audit_checks import measure_owned_act_drought
    from maestro.memory import JsonlVersionArchiveLedger

    data_dir = Path("/fake/data")
    _seed_stand_downs(mem_fs, data_dir, [{
        "pr": 3179, "head_sha": "abc",
        "recorded_at": "2026-07-03T00:00:00+00:00",
        "branch_ahead_of_origin": True,
    }])
    _seed_obsolete_plans(mem_fs, data_dir, [{
        "plan_id": "abc123def456", "status": "obsolete",
        "reviewed_at": "2026-07-01T00:00:00+00:00",
    }])
    mem_fs[str(data_dir / "task_history.jsonl")] = json.dumps(
        {"timestamp": "2026-06-01T00:00:00+00:00"},
    ) + "\n"

    sd_before = mem_fs[str(data_dir / "stand_down_verdicts.jsonl")]
    plans_before = mem_fs[str(data_dir / "evolution_plans.jsonl")]
    before = measure_owned_act_drought(
        data_dir, window=50,
        stand_down_backend=JsonlStandDownVerdictLedger(data_dir),
        version_archive_backend=JsonlVersionArchiveLedger(data_dir),
    )

    format_owned_acts_report(data_dir, 0, as_json=False, stand_down_backend=JsonlStandDownVerdictLedger(data_dir), deed_backend=JsonlDeedLedger(data_dir))
    format_owned_acts_report(data_dir, 0, as_json=True, stand_down_backend=JsonlStandDownVerdictLedger(data_dir), deed_backend=JsonlDeedLedger(data_dir))

    after = measure_owned_act_drought(
        data_dir, window=50,
        stand_down_backend=JsonlStandDownVerdictLedger(data_dir),
        version_archive_backend=JsonlVersionArchiveLedger(data_dir),
    )
    assert after == before == 2
    # The additive readers never rewrite their source files.
    assert mem_fs[str(data_dir / "stand_down_verdicts.jsonl")] == sd_before
    assert mem_fs[str(data_dir / "evolution_plans.jsonl")] == plans_before


# ──────────────────────────────────────────────────────────────────────
# Preflight-pass ledger — home-module adapter + seed source (proposal 106)
# ──────────────────────────────────────────────────────────────────────


class TestJsonPreflightPassLedger:
    """The default flag-off preflight-pass backend wrapping the JSON core."""

    def test_both_adapters_are_runtime_checkable(self) -> None:
        # ``@runtime_checkable`` lets the agent's backend-selection tests assert
        # structural conformance for either substrate.
        assert isinstance(
            JsonPreflightPassLedger(Path("/fake/rc")), PreflightPassBackend,
        )
        assert isinstance(
            SqlitePreflightPassStore(StateStore(":memory:")),
            PreflightPassBackend,
        )

    def test_record_and_should_skip_delegate(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/json-pf")
        ledger = JsonPreflightPassLedger(data_dir)
        ledger.record(7, "head7")
        assert ledger.should_skip(7, "head7") is True
        # Persisted to the same JSON file the free functions own.
        assert str(data_dir / "preflight_passes.json") in mem_fs
        # A mismatched head does not skip.
        assert ledger.should_skip(7, "other") is False

    def test_record_replaces_prior_head(self, mem_fs: dict[str, str]) -> None:
        data_dir = Path("/fake/json-pf-replace")
        ledger = JsonPreflightPassLedger(data_dir)
        ledger.record(7, "old")
        ledger.record(7, "new")
        # Only the latest recorded head skips — a per-PR keyed replace.
        assert ledger.should_skip(7, "old") is False
        assert ledger.should_skip(7, "new") is True

    def test_should_skip_empty_head_never_skips(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/json-pf-empty")
        ledger = JsonPreflightPassLedger(data_dir)
        ledger.record(7, "head7")
        # An unresolvable head must never suppress remediation.
        assert ledger.should_skip(7, "") is False

    def test_should_skip_absent_ledger_returns_false(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonPreflightPassLedger(Path("/fake/json-pf-absent"))
        assert ledger.should_skip(1, "h1") is False


def test_load_all_preflight_passes_is_sorted_by_pr_number(
    mem_fs: dict[str, str],
) -> None:
    """The SQLite seed source returns records sorted by ``pr_number``."""
    data_dir = Path("/fake/pf-loadall")
    record_preflight_pass(10, "sha-a", data_dir)
    record_preflight_pass(2, "sha-b", data_dir)
    assert load_all_preflight_passes(data_dir) == [
        PreflightPassRecord(pr_number=2, head_sha="sha-b"),
        PreflightPassRecord(pr_number=10, head_sha="sha-a"),
    ]


def test_load_all_preflight_passes_absent_file_is_empty() -> None:
    assert load_all_preflight_passes(Path("/fake/pf-loadall-absent")) == []


def test_load_all_preflight_passes_corrupt_json_is_empty(
    mem_fs: dict[str, str],
) -> None:
    data_dir = Path("/fake/pf-corrupt")
    mem_fs[str(data_dir / "preflight_passes.json")] = "not-json{"
    assert load_all_preflight_passes(data_dir) == []


def test_load_all_preflight_passes_skips_non_integer_key(
    mem_fs: dict[str, str],
) -> None:
    """A hand-edited non-integer PR key is dropped, not crashed on."""
    data_dir = Path("/fake/pf-badkey")
    mem_fs[str(data_dir / "preflight_passes.json")] = json.dumps(
        {"3": "ok", "notanint": "sha-c"},
    )
    assert load_all_preflight_passes(data_dir) == [
        PreflightPassRecord(pr_number=3, head_sha="ok"),
    ]


def test_load_all_preflight_passes_skips_non_string_head(
    mem_fs: dict[str, str],
) -> None:
    """A non-string head value is dropped by the tolerant JSON loader."""
    data_dir = Path("/fake/pf-nonstr")
    mem_fs[str(data_dir / "preflight_passes.json")] = json.dumps(
        {"1": "ok", "2": 123},
    )
    assert load_all_preflight_passes(data_dir) == [
        PreflightPassRecord(pr_number=1, head_sha="ok"),
    ]


def test_json_and_sqlite_preflight_agree(mem_fs: dict[str, str]) -> None:
    """Both backends answer ``should_skip`` identically for the same inputs.

    The proposal-106 substrate-parity contract for the preflight-pass ledger: a
    head recorded through either backend reads back identically, so the tend
    dequeue's preflight-pass SHA gate is invariant across
    ``[paths] sqlite_state_enabled``.
    """
    data_dir = Path("/fake/pf-parity")
    json_ledger = JsonPreflightPassLedger(data_dir)
    sqlite = SqlitePreflightPassStore(StateStore(":memory:"))
    for pr, head in [(1, "h1"), (2, "h2"), (1, "h1b")]:  # PR 1 re-recorded
        json_ledger.record(pr, head)
        sqlite.record(pr, head)
    for pr, head in [
        (1, "h1"),      # superseded head → no skip on both
        (1, "h1b"),     # latest head → skip on both
        (2, "h2"),      # match
        (2, "other"),   # mismatched head
        (2, ""),        # empty head → never skip
        (99, "h1"),     # unknown pr
    ]:
        assert json_ledger.should_skip(pr, head) == sqlite.should_skip(pr, head)


def test_json_and_sqlite_preflight_agree_via_legacy_seed(
    mem_fs: dict[str, str],
) -> None:
    """SQLite seeded from the JSON ledger agrees with the JSON reader."""
    data_dir = Path("/fake/pf-parity-seed")
    record_preflight_pass(1, "h1", data_dir)
    record_preflight_pass(2, "h2", data_dir)
    json_ledger = JsonPreflightPassLedger(data_dir)
    sqlite = SqlitePreflightPassStore(
        StateStore(":memory:"), legacy_json_dir=data_dir,
    )
    for pr, head in [(1, "h1"), (2, "h2"), (1, "wrong"), (2, "")]:
        assert json_ledger.should_skip(pr, head) == sqlite.should_skip(pr, head)


def test_stand_down_backends_parity_across_verdict_release_reverdict(
    mem_fs: dict[str, str],
) -> None:
    """JSONL and SQLite stand-down backends agree on every read shape.

    Drives the same verdict → release → re-verdict sequence through the free
    functions over both a :class:`JsonlStandDownVerdictLedger` and a
    :class:`~maestro.state_store.SqliteStandDownVerdictLedger`, then asserts the
    dequeue find, the head-agnostic liveness, the OwnedAct projection, the
    written count, the banked-PR set, and the drought-timestamp list all match —
    the proposal-106 flag-parity guarantee at the remediation-tracker layer.
    """
    jsonl = JsonlStandDownVerdictLedger(Path("/fake/rt-parity"))
    sqlite = SqliteStandDownVerdictLedger(StateStore(":memory:"))

    for backend in (jsonl, sqlite):
        record_stand_down(7, "a", backend, now="t1", branch_ahead_of_origin=True)
        record_stand_down(7, "b", backend, now="t2")
        release_stand_downs(7, backend, now="t3")
        record_stand_down(7, "c", backend, now="t4", branch_ahead_of_origin=False)
        record_stand_down(8, "z", backend, now="t5", branch_ahead_of_origin=True)

    # The release voided the a/b verdicts head-agnostically; only c survives.
    assert find_stand_down(7, "a", jsonl) == find_stand_down(7, "a", sqlite) is None
    assert find_stand_down(7, "c", jsonl) == find_stand_down(7, "c", sqlite)
    assert find_stand_down(7, "c", jsonl) is not None
    assert has_live_stand_down(7, jsonl) == has_live_stand_down(7, sqlite) is True
    assert read_chosen_stand_down_acts(jsonl) == read_chosen_stand_down_acts(sqlite)
    tasks: list[dict[str, object]] = []
    assert measure_standdown_production(tasks, jsonl) == (
        measure_standdown_production(tasks, sqlite)
    )
    assert jsonl.banked_pr_ids() == sqlite.banked_pr_ids() == {7, 8}
    assert jsonl.recorded_timestamps() == sqlite.recorded_timestamps()


# ──────────────────────────────────────────────────────────────────────
# Remediation tracker — parse core, seed source, JSON adapter (proposal 106)
# ──────────────────────────────────────────────────────────────────────


class TestLoadTrackerState:
    """The shared seed source both substrates read the legacy sidecar through."""

    def test_absent_file_is_a_fresh_tracker(self, mem_fs: dict[str, str]) -> None:
        # No file on disk → empty maps and version 0, so the one-time #2796
        # merge-block reconciliation never fires for a fresh tracker.
        state = load_tracker_state(Path("/fake/absent"))
        assert state == TrackerState({}, {}, {}, {}, 0)

    def test_corrupt_file_is_a_fresh_tracker(self, mem_fs: dict[str, str]) -> None:
        # Unparseable JSON is tolerated, not raised — same as the historical load.
        data_dir = Path("/fake/corrupt")
        mem_fs[str(data_dir / "remediation_tracker.json")] = "{not json"
        assert load_tracker_state(data_dir) == TrackerState({}, {}, {}, {}, 0)

    def test_non_mapping_payload_is_a_fresh_tracker(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Valid JSON that is not an object (a list) also reads as version 0.
        data_dir = Path("/fake/list")
        mem_fs[str(data_dir / "remediation_tracker.json")] = json.dumps([1, 2])
        assert load_tracker_state(data_dir) == TrackerState({}, {}, {}, {}, 0)

    def test_legacy_flat_format_reads_as_version_one(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A pre-#2744 flat file (PR key → outcome list) keeps its outcomes, has
        # no items/skips maps, and reads as the legacy version so the one-time
        # merge-block reconciliation is still owed.
        data_dir = Path("/fake/flat")
        mem_fs[str(data_dir / "remediation_tracker.json")] = json.dumps(
            {"42": [True, False], "43": "not-a-list"},
        )
        state = load_tracker_state(data_dir)
        assert state == TrackerState({"42": [True, False]}, {}, {}, {}, 1)

    def test_wrapper_format_round_trips_every_facet(
        self, mem_fs: dict[str, str],
    ) -> None:
        data_dir = Path("/fake/wrapper")
        mem_fs[str(data_dir / "remediation_tracker.json")] = json.dumps(
            {
                "outcomes": {"42": [True]},
                "items_remaining": {"42": 3},
                "skips": {"42": 2},
                "merge_block_skips": {"42": 1},
                "schema_version": 2,
            },
        )
        state = load_tracker_state(data_dir)
        assert state == TrackerState({"42": [True]}, {"42": 3}, {"42": 2}, {"42": 1}, 2)

    def test_wrapper_without_version_reads_as_legacy(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A wrapper file predating the version stamp is a pre-#2796 file.
        data_dir = Path("/fake/unversioned")
        mem_fs[str(data_dir / "remediation_tracker.json")] = json.dumps(
            {"outcomes": {"42": [True]}},
        )
        assert load_tracker_state(data_dir).schema_version == 1

    @pytest.mark.parametrize("bogus", [True, "2", None, 1.5])
    def test_non_integer_version_reads_as_legacy(self, bogus: object) -> None:
        # ``bool`` subclasses ``int`` — it must not pose as a version either.
        payload = {"outcomes": {}, "schema_version": bogus}
        assert _parse_tracker_payload(payload).schema_version == 1

    def test_structurally_wrong_values_are_dropped_not_raised(self) -> None:
        # Every map drops entries whose value has the wrong shape, and ``bool``
        # is excluded from the three integer maps.
        payload = {
            "outcomes": {"1": [True], "2": ["x"], "3": "y"},
            "items_remaining": {"1": 4, "2": True, "3": "z"},
            "skips": {"1": 5, "2": False},
            "merge_block_skips": {"1": 6, "2": "w"},
            "schema_version": 2,
        }
        assert _parse_tracker_payload(payload) == TrackerState(
            {"1": [True]}, {"1": 4}, {"1": 5}, {"1": 6}, 2,
        )

    def test_non_mapping_facets_are_ignored(self) -> None:
        # A wrapper whose sibling maps are not dicts yields empty maps, never a raise.
        payload = {
            "outcomes": {"1": [False]},
            "items_remaining": ["nope"],
            "skips": 7,
            "merge_block_skips": None,
            "schema_version": 2,
        }
        assert _parse_tracker_payload(payload) == TrackerState(
            {"1": [False]}, {}, {}, {}, 2,
        )


class TestJsonRemediationTrackerStore:
    """The default flag-off tracker backend over ``remediation_tracker.json``."""

    def test_adapter_is_runtime_checkable(self) -> None:
        # ``@runtime_checkable`` lets the agent's backend-selection tests assert
        # structural conformance without importing the concrete class.
        assert isinstance(
            JsonRemediationTrackerStore(Path("/fake/rc")),
            RemediationTrackerBackend,
        )

    def test_load_on_absent_file(self, mem_fs: dict[str, str]) -> None:
        store = JsonRemediationTrackerStore(Path("/fake/json-tracker"))
        assert store.load() == TrackerState({}, {}, {}, {}, 0)

    def test_persist_writes_the_wrapper_format(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The adapter keeps the historical on-disk shape byte-for-byte, so a
        # flag-off rollback reads exactly what it wrote before the seam.
        data_dir = Path("/fake/json-tracker")
        store = JsonRemediationTrackerStore(data_dir)
        store.persist(
            TrackerState({"42": [True]}, {"42": 3}, {"42": 2}, {"42": 1}, 2),
        )
        written = json.loads(mem_fs[str(data_dir / "remediation_tracker.json")])
        assert written == {
            "outcomes": {"42": [True]},
            "items_remaining": {"42": 3},
            "skips": {"42": 2},
            "merge_block_skips": {"42": 1},
            "schema_version": 2,
        }

    def test_persist_then_load_round_trips(self, mem_fs: dict[str, str]) -> None:
        store = JsonRemediationTrackerStore(Path("/fake/json-rt"))
        state = TrackerState({"7": [False, True]}, {"7": 1}, {"8": 4}, {"9": 5}, 2)
        store.persist(state)
        assert store.load() == state

    def test_persist_replaces_whole_state(self, mem_fs: dict[str, str]) -> None:
        # Whole-file replace: a key dropped from the state leaves no residue.
        store = JsonRemediationTrackerStore(Path("/fake/json-replace"))
        store.persist(TrackerState({"1": [True], "2": [False]}, {}, {}, {}, 2))
        store.persist(TrackerState({"2": [False]}, {}, {}, {}, 2))
        assert store.load().outcomes == {"2": [False]}


def test_tracker_defaults_to_the_json_backend(mem_fs: dict[str, str]) -> None:
    # Constructed without a backend, the tracker builds the JSON adapter over
    # its own data dir — the flag-off default that keeps today's sidecar.
    tracker = RemediationTracker(Path("/fake/default"))
    assert isinstance(tracker._backend, JsonRemediationTrackerStore)


def test_tracker_reads_and_writes_through_an_injected_backend(
    mem_fs: dict[str, str],
) -> None:
    # An injected backend fully owns persistence: the tracker seeds from its
    # ``load`` and every mutation lands in its ``persist``, with no JSON file
    # written under the data dir.
    class _RecordingBackend:
        def __init__(self) -> None:
            self.state = TrackerState({"42": [True]}, {"42": 2}, {}, {}, 2)
            self.persisted: list[TrackerState] = []

        def load(self) -> TrackerState:
            return self.state

        def persist(self, state: TrackerState) -> None:
            self.persisted.append(state)

    backend = _RecordingBackend()
    tracker = RemediationTracker(Path("/fake/injected"), backend=backend)
    assert tracker.noop_count(42) == 1
    assert tracker.last_items_remaining(42) == 2
    tracker.record_outcome(42, noop=True)
    assert backend.persisted[-1].outcomes == {"42": [True, True]}
    assert backend.persisted[-1].schema_version == 2
    assert str(Path("/fake/injected") / "remediation_tracker.json") not in mem_fs


def test_tracker_json_and_sqlite_backends_are_object_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same mutation sequence yields identical reads through both backends.

    Walks the tracker's whole mutating surface — productive and no-op outcomes
    with and without an items baseline, a merge-block deferral, a repetition
    skip, ``reset``, and ``evict_settled`` — asserting the four gate readers
    agree after every step, plus a reload-from-store agreement so the parity is
    of persisted state and not merely of the in-memory caches.
    """
    json_tracker = RemediationTracker(Path("/fake/parity-json"), threshold=2)
    store = StateStore(":memory:")
    sqlite_tracker = RemediationTracker(
        Path("/fake/parity-sqlite"),
        threshold=2,
        backend=SqliteRemediationTrackerStore(store),
    )
    both = (json_tracker, sqlite_tracker)

    def _readings(tracker: RemediationTracker) -> tuple[object, ...]:
        return tuple(
            (
                tracker.should_reroute(pr),
                tracker.noop_count(pr),
                tracker.skip_count(pr),
                tracker.merge_block_skip_count(pr),
                tracker.last_items_remaining(pr),
            )
            for pr in (11, 12)
        )

    def _assert_parity() -> None:
        assert _readings(json_tracker) == _readings(sqlite_tracker)
        # Reload from each substrate: the persisted state agrees too.
        reloaded_json = RemediationTracker(Path("/fake/parity-json"), threshold=2)
        reloaded_sqlite = RemediationTracker(
            Path("/fake/parity-sqlite"),
            threshold=2,
            backend=SqliteRemediationTrackerStore(store),
        )
        assert _readings(reloaded_json) == _readings(reloaded_sqlite)
        assert _readings(reloaded_json) == _readings(json_tracker)

    _assert_parity()

    for tracker in both:
        tracker.record_outcome(11, noop=True)
    _assert_parity()

    for tracker in both:
        tracker.record_outcome(11, noop=True, items_remaining=4)
    _assert_parity()

    for tracker in both:
        tracker.record_outcome(12, noop=False, items_remaining=2)
    _assert_parity()

    for tracker in both:
        tracker.record_outcome(12, noop=True, merge_block=True)
    _assert_parity()

    for tracker in both:
        assert tracker.record_skip(12) == 1
    _assert_parity()

    for tracker in both:
        tracker.reset(11)
    _assert_parity()

    for tracker in both:
        assert tracker.evict_settled(12) is True
    _assert_parity()

    # Both stores are genuinely empty after the last eviction — no orphan rows
    # on either substrate.
    assert json.loads(
        mem_fs[str(Path("/fake/parity-json") / "remediation_tracker.json")],
    )["outcomes"] == {}
    assert store.load_remediation_tracker().outcomes == {}


def test_merge_block_migration_is_parity_across_substrates(
    mem_fs: dict[str, str],
) -> None:
    """An identical pre-v2 payload fires the reconciliation on both substrates.

    Each substrate gets its own copy of the same pre-v2 sidecar (a shared one
    would be rewritten by whichever tracker constructs first, so the second
    would seed from already-migrated state and prove nothing). The JSON tracker
    reads its copy directly; the SQLite adapter seeds ``state.db`` from its
    copy, carrying the pre-v2 ``schema_version`` across — so the
    merge-block-poisoned PR is freed and stamped v2 on either store rather than
    the migration silently skipping after the flag flip.
    """
    payload = json.dumps(
        {"outcomes": {"2736": [True, True]}, "schema_version": 1},
    )
    history = "".join(
        json.dumps(t) + "\n" for t in (_mb_task(2736), _mb_task(2736))
    )
    json_dir = Path("/fake/migration-parity-json")
    sqlite_dir = Path("/fake/migration-parity-sqlite")
    for data_dir in (json_dir, sqlite_dir):
        mem_fs[str(data_dir / "remediation_tracker.json")] = payload
        mem_fs[str(data_dir / "task_history.jsonl")] = history

    json_tracker = RemediationTracker(json_dir, threshold=2)
    store = StateStore(":memory:")
    sqlite_tracker = RemediationTracker(
        sqlite_dir,
        threshold=2,
        backend=SqliteRemediationTrackerStore(
            store, legacy_json_dir=sqlite_dir,
        ),
    )
    # Both freed the PR whose only no-op laps were merge-block deferrals.
    assert json_tracker.should_reroute(2736) is True
    assert sqlite_tracker.should_reroute(2736) is True
    # Both stamped v2, so neither re-runs the migration on the next load.
    assert load_tracker_state(json_dir).schema_version == 2
    assert store.load_remediation_tracker().schema_version == 2
    # A fresh genuine no-op streak recorded after the migration is not clawed
    # back on either substrate — the version bump prevents a second run.
    for tracker in (json_tracker, sqlite_tracker):
        tracker.record_outcome(2736, noop=True)
        tracker.record_outcome(2736, noop=True)
        assert tracker.should_reroute(2736) is False
    reloaded_sqlite = RemediationTracker(
        sqlite_dir,
        threshold=2,
        backend=SqliteRemediationTrackerStore(store, legacy_json_dir=sqlite_dir),
    )
    assert RemediationTracker(json_dir, threshold=2).should_reroute(2736) is False
    assert reloaded_sqlite.should_reroute(2736) is False


class TestLatestPrReadinessSnapshots:
    """The shared latest-snapshot-per-PR reader lifted out of the churn line (135)."""

    def test_keys_snapshots_by_the_remediation_goal(self) -> None:
        history: list[dict[str, object]] = [
            {"goal": "Remediate PR #12: fix it", "pr_readiness_snapshot": {"a": 1}},
        ]
        assert latest_pr_readiness_snapshots(history) == {12: {"a": 1}}

    def test_falls_back_to_the_title(self) -> None:
        """An older record carries its subject in the title, not the goal."""
        history: list[dict[str, object]] = [
            {
                "goal": "",
                "title": "Remediate PR #13: fix it",
                "pr_readiness_snapshot": {"b": 2},
            },
            {"title": "Remediate PR #14: fix it", "pr_readiness_snapshot": {"c": 3}},
        ]
        assert latest_pr_readiness_snapshots(history) == {13: {"b": 2}, 14: {"c": 3}}

    def test_last_write_wins_per_pr(self) -> None:
        history: list[dict[str, object]] = [
            {"goal": "Remediate PR #12: a", "pr_readiness_snapshot": {"n": 1}},
            {"goal": "Remediate PR #12: b", "pr_readiness_snapshot": {"n": 2}},
        ]
        assert latest_pr_readiness_snapshots(history) == {12: {"n": 2}}

    def test_skips_unmatched_and_malformed_records(self) -> None:
        history: list[dict[str, object]] = [
            {"goal": "Write a doc", "pr_readiness_snapshot": {"x": 1}},
            {"goal": "Remediate PR #12: a", "pr_readiness_snapshot": "not-a-dict"},
            {"goal": "Remediate PR #13: b"},
            {"pr_readiness_snapshot": {"y": 1}},
            {"goal": 17, "title": None, "pr_readiness_snapshot": {"z": 1}},
        ]
        assert latest_pr_readiness_snapshots(history) == {}

    def test_empty_history_yields_empty(self) -> None:
        assert latest_pr_readiness_snapshots([]) == {}

    def test_not_before_skips_a_record_older_than_the_floor(self) -> None:
        """A snapshot predating the floor is dropped, not read as current."""
        history: list[dict[str, object]] = [
            {
                "goal": "Remediate PR #12: a",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "pr_readiness_snapshot": {"n": 1},
            },
        ]
        floor = {12: "2026-01-02T00:00:00+00:00"}
        assert latest_pr_readiness_snapshots(history, not_before=floor) == {}

    def test_not_before_keeps_a_record_at_or_after_the_floor(self) -> None:
        history: list[dict[str, object]] = [
            {
                "goal": "Remediate PR #12: a",
                "timestamp": "2026-01-02T00:00:00+00:00",
                "pr_readiness_snapshot": {"n": 1},
            },
        ]
        floor = {12: "2026-01-02T00:00:00+00:00"}
        assert latest_pr_readiness_snapshots(history, not_before=floor) == {
            12: {"n": 1},
        }

    def test_not_before_drops_an_unstamped_record(self) -> None:
        """A record with no parseable timestamp cannot be proven post-floor."""
        history: list[dict[str, object]] = [
            {"goal": "Remediate PR #12: a", "pr_readiness_snapshot": {"n": 1}},
        ]
        floor = {12: "2026-01-02T00:00:00+00:00"}
        assert latest_pr_readiness_snapshots(history, not_before=floor) == {}

    def test_unparseable_or_absent_floor_leaves_the_pr_unfiltered(self) -> None:
        """The tolerant direction: an unreadable bound must not blank a reading."""
        history: list[dict[str, object]] = [
            {"goal": "Remediate PR #12: a", "pr_readiness_snapshot": {"n": 1}},
            {"goal": "Remediate PR #13: b", "pr_readiness_snapshot": {"m": 2}},
        ]
        floor = {12: "not-a-timestamp"}
        assert latest_pr_readiness_snapshots(history, not_before=floor) == {
            12: {"n": 1},
            13: {"m": 2},
        }


class TestRefutedStanddownIsNotScoredQuiet:
    """Negative control: the refutation annotation is not a stand-down marker.

    The rendering and the quiet-scoring sensors share this module, and both
    scorers match ``_CHOSEN_QUIET_MARKER`` by substring — so a refutation tag
    that shared the marker's spelling would let a *contradicted* latch be scored
    as a deliberate quiet act, exactly inverting what it measures.
    """

    def test_not_classified_stood_down(self) -> None:
        state = classify_remediation_outcome_state(
            "[standdown-refuted] owned work reappeared under the latch",
            "",
            merge_block_reason=None,
        )
        assert state != "STOOD_DOWN"

    def test_not_scored_as_a_chosen_quiet(self) -> None:
        ratio = measure_forced_vs_chosen_quiet(
            [
                {
                    "goal": "Remediate PR #12: fix it",
                    "result_summary": "[standdown-refuted] owned work reappeared",
                },
            ],
        )
        assert ratio.chosen == 0

    def test_a_genuine_marker_still_scores_chosen(self) -> None:
        """The control is not vacuous — the real marker is still recognised."""
        ratio = measure_forced_vs_chosen_quiet(
            [
                {
                    "goal": "Remediate PR #12: fix it",
                    "result_summary": "[stood-down] nothing left to do",
                },
            ],
        )
        assert ratio.chosen == 1


# ---------------------------------------------------------------------------
# Proposal 137 — owed-act divergence over tend cooldown skips
# ---------------------------------------------------------------------------


class _OwedActGateBackend:
    """In-memory ``GateActivationBackend`` over a fixed row list.

    Structural stand-in for the flag-selected ledger so the sensor tests touch
    no filesystem. ``read_all`` mirrors the real adapters' full load.
    """

    def __init__(self, rows: list[GateActivation]) -> None:
        self.rows = list(rows)

    def append(self, record: GateActivation) -> None:
        self.rows.append(record)

    def read_all(self) -> list[GateActivation]:
        return list(self.rows)

    def read_tail(self, limit: int) -> list[GateActivation]:
        return list(self.rows)[-limit:] if limit > 0 else []

    def read_gate_tail(self, gate: str, limit: int) -> list[GateActivation]:
        rows = [r for r in self.rows if r.gate == gate]
        return rows[-limit:] if limit > 0 else []


def _skip_row(
    pr_number: int,
    owed: str,
    *,
    cooldown: str = "conflict",
    gate: str = COOLDOWN_SKIP_GATE,
) -> GateActivation:
    """Build one ``tend_cooldown_skip`` row through the shared renderer."""
    return GateActivation(
        gate=gate,
        action="diverged" if owed == "code-owed" else "aligned",
        timestamp="2026-09-23T00:00:00+00:00",
        detail=format_cooldown_skip_detail(pr_number, cooldown, owed),
    )


class TestFormatCooldownSkipDetail:
    """The one renderer both stamp sites write and the sensor parses back."""

    def test_renders_the_three_fields(self) -> None:
        """PR, cooldown kind and owed act appear in the documented shape."""
        detail = format_cooldown_skip_detail(42, "quality_fix", "code-owed")
        assert detail == "pr=#42 cooldown=quality_fix owed=code-owed"

    def test_empty_fields_render_as_a_placeholder(self) -> None:
        """An unmeasured owed act or cooldown renders ``-``, never an empty run.

        The parse pattern requires non-space runs, so a blank field must not
        collapse two separators together and make the row unparseable.
        """
        detail = format_cooldown_skip_detail(7, "", "")
        assert detail == "pr=#7 cooldown=- owed=-"

    def test_stays_under_the_detail_truncation(self) -> None:
        """Even an absurd cooldown name fits ``log_gate_activation``'s 200 chars."""
        detail = format_cooldown_skip_detail(999999, "conflict", "handoff-ready")
        assert len(detail) < 200

    def test_renderer_output_round_trips_through_the_sensor(self) -> None:
        """Writer and reader agree: what the renderer emits, the sensor parses."""
        backend = _OwedActGateBackend([_skip_row(11, "code-owed")])
        measured = measure_owed_act_divergence(backend, 10)
        assert measured == OwedActDivergence(
            diverged=1, aligned=0, total=1, prs=(11,),
            diverged_by_kind=(("cooldown", 1), ("disposal", 0), ("handoff", 0)),
        )


class TestMeasureOwedActDivergence:
    """The classification leg over the gate-activation trace."""

    def test_empty_corpus_reads_zero(self) -> None:
        """No rows at all yields an all-zero reading, not an error."""
        measured = measure_owed_act_divergence(_OwedActGateBackend([]), 10)
        assert measured == OwedActDivergence(
            diverged=0, aligned=0, total=0, prs=(),
        )

    def test_mixed_window_classifies_and_dedupes_prs(self) -> None:
        """Code-owed skips diverge, others align, and offender PRs de-duplicate."""
        backend = _OwedActGateBackend([
            _skip_row(5, "code-owed"),
            _skip_row(5, "code-owed", cooldown="quality_fix"),
            _skip_row(3, "wait-ci"),
            _skip_row(9, "code-owed"),
            _skip_row(4, "handoff-ready"),
        ])
        measured = measure_owed_act_divergence(backend, 50)
        assert measured == OwedActDivergence(
            diverged=3, aligned=2, total=5, prs=(5, 9),
            diverged_by_kind=(("cooldown", 3), ("disposal", 0), ("handoff", 0)),
        )

    def test_rows_from_another_gate_are_skipped(self) -> None:
        """Only ``tend_cooldown_skip`` rows are classified."""
        backend = _OwedActGateBackend([
            _skip_row(1, "code-owed", gate="plan_grounding"),
            _skip_row(2, "code-owed"),
        ])
        measured = measure_owed_act_divergence(backend, 50)
        assert measured == OwedActDivergence(
            diverged=1, aligned=0, total=1, prs=(2,),
            diverged_by_kind=(("cooldown", 1), ("disposal", 0), ("handoff", 0)),
        )

    def test_unparseable_detail_is_skipped(self) -> None:
        """A detail the renderer did not produce manufactures no divergence."""
        backend = _OwedActGateBackend([
            GateActivation(
                gate=COOLDOWN_SKIP_GATE,
                action="diverged",
                timestamp="2026-09-23T00:00:00+00:00",
                detail="cooldown held for a bit",
            ),
        ])
        measured = measure_owed_act_divergence(backend, 50)
        assert measured == OwedActDivergence(
            diverged=0, aligned=0, total=0, prs=(),
        )

    def test_non_string_fields_are_skipped(self) -> None:
        """A structurally-corrupt row is dropped rather than guessed at."""
        backend = _OwedActGateBackend([
            GateActivation(
                gate=None,  # type: ignore[arg-type]
                action="diverged",
                timestamp="2026-09-23T00:00:00+00:00",
                detail="pr=#1 cooldown=conflict owed=code-owed",
            ),
            GateActivation(
                gate=COOLDOWN_SKIP_GATE,
                action="diverged",
                timestamp="2026-09-23T00:00:00+00:00",
                detail=None,  # type: ignore[arg-type]
            ),
        ])
        measured = measure_owed_act_divergence(backend, 50)
        assert measured == OwedActDivergence(
            diverged=0, aligned=0, total=0, prs=(),
        )

    def test_non_positive_window_reads_nothing(self) -> None:
        """``n <= 0`` reads no rows at all."""
        backend = _OwedActGateBackend([_skip_row(1, "code-owed")])
        measured = measure_owed_act_divergence(backend, 0)
        assert measured == OwedActDivergence(
            diverged=0, aligned=0, total=0, prs=(),
        )

    def test_window_bounds_the_read(self) -> None:
        """Only the trailing *n* rows are classified."""
        backend = _OwedActGateBackend([
            _skip_row(1, "code-owed"),
            _skip_row(2, "wait-ci"),
            _skip_row(3, "wait-ci"),
        ])
        measured = measure_owed_act_divergence(backend, 2)
        assert measured == OwedActDivergence(
            diverged=0, aligned=2, total=2, prs=(),
        )

    def test_unrelated_gate_rows_do_not_dilute_the_window(self) -> None:
        """The window counts skips, not raw rows, so chatter cannot evict them."""
        noise = [_skip_row(9, "code-owed", gate="other_gate") for _ in range(30)]
        backend = _OwedActGateBackend(
            [_skip_row(1, "code-owed"), _skip_row(2, "wait-ci"), *noise],
        )
        measured = measure_owed_act_divergence(backend, 2)
        assert measured == OwedActDivergence(
            diverged=1, aligned=1, total=2, prs=(1,),
            diverged_by_kind=(("cooldown", 1), ("disposal", 0), ("handoff", 0)),
        )

    def test_chatter_beyond_any_raw_tail_does_not_hide_skips(self) -> None:
        """Skips buried under far more unrelated rows than *n* are still read.

        The filter runs over the whole trace, not a bounded raw tail, so a
        chatty stretch of other gates cannot push the skips out of view.
        """
        noise = [_skip_row(9, "code-owed", gate="other_gate") for _ in range(500)]
        backend = _OwedActGateBackend(
            [_skip_row(1, "code-owed"), _skip_row(2, "wait-ci"), *noise],
        )
        measured = measure_owed_act_divergence(backend, 2)
        assert measured == OwedActDivergence(
            diverged=1, aligned=1, total=2, prs=(1,),
            diverged_by_kind=(("cooldown", 1), ("disposal", 0), ("handoff", 0)),
        )

    def test_measure_writes_nothing_to_disk(self, mem_fs: dict[str, str]) -> None:
        """The sensor is a pure read — no file is created or mutated."""
        before = dict(mem_fs)
        backend = _OwedActGateBackend([_skip_row(1, "code-owed")])
        measure_owed_act_divergence(backend, 10)
        assert mem_fs == before


class TestFormatOwedActDivergence:
    """The rendering leg, including both self-suppression conditions."""

    def test_offender_list_is_capped(self) -> None:
        """More than five offending PRs render as the first five plus ``+N more``."""
        rows = [_skip_row(pr, "code-owed") for pr in range(1, 8)]
        line = format_owed_act_divergence(_OwedActGateBackend(rows), 50)
        assert "(#1, #2, #3, #4, #5, +2 more)" in line

    def test_below_the_data_floor_suppresses(self) -> None:
        """A window too thin to read returns the empty string."""
        rows = [
            _skip_row(1, "code-owed")
            for _ in range(_OWED_ACT_DIVERGENCE_DATA_FLOOR - 1)
        ]
        assert format_owed_act_divergence(_OwedActGateBackend(rows), 50) == ""

    def test_at_the_data_floor_surfaces(self) -> None:
        """Exactly at the floor the line appears — the bound is inclusive."""
        rows = [
            _skip_row(pr, "code-owed")
            for pr in range(1, _OWED_ACT_DIVERGENCE_DATA_FLOOR + 1)
        ]
        line = format_owed_act_divergence(_OwedActGateBackend(rows), 50)
        assert line.startswith(
            f"OWED-ACT-DIVERGENCE: {_OWED_ACT_DIVERGENCE_DATA_FLOOR}/"
            f"{_OWED_ACT_DIVERGENCE_DATA_FLOOR} owed-act decisions (cooldown "
            f"{_OWED_ACT_DIVERGENCE_DATA_FLOOR})",
        )

    def test_zero_divergence_self_extinguishes(self) -> None:
        """A clean reading above the floor stays dark rather than being noise."""
        rows = [
            _skip_row(1, "wait-ci")
            for _ in range(_OWED_ACT_DIVERGENCE_DATA_FLOOR + 3)
        ]
        assert format_owed_act_divergence(_OwedActGateBackend(rows), 50) == ""

    def test_line_names_the_offending_prs(self) -> None:
        """Offenders are parenthesised, sorted, and prefixed with ``#``."""
        rows = [_skip_row(9, "code-owed"), _skip_row(2, "code-owed")]
        rows += [
            _skip_row(3, "wait-ci")
            for _ in range(_OWED_ACT_DIVERGENCE_DATA_FLOOR)
        ]
        line = format_owed_act_divergence(_OwedActGateBackend(rows), 50)
        assert "(#2, #9)" in line

    def test_line_disclaims_the_stronger_reading(self) -> None:
        """The em-dash clause says what the reading does not claim."""
        rows = [
            _skip_row(pr, "code-owed")
            for pr in range(1, _OWED_ACT_DIVERGENCE_DATA_FLOOR + 1)
        ]
        line = format_owed_act_divergence(_OwedActGateBackend(rows), 50)
        assert "does not claim the decision was wrong" in line

    def test_default_window_is_the_module_constant(self) -> None:
        """The default *n* is the declared window, not a literal at the call site."""
        captured: list[int] = []

        def _spy(backend: object, n: int) -> OwedActDivergence:
            captured.append(n)
            return OwedActDivergence(diverged=0, aligned=0, total=0, prs=())

        with patch.object(
            remediation_tracker, "measure_owed_act_divergence", _spy,
        ):
            format_owed_act_divergence(_OwedActGateBackend([]))
        assert captured == [_OWED_ACT_DIVERGENCE_WINDOW]


class TestOwedActDivergenceInKnownStateSummary:
    """The line reaches contemplation and task generation through known-state."""

    def test_line_appears_when_the_backend_diverges(self) -> None:
        """A diverging trace injects the line with no ``data_dir`` needed."""
        rows = [
            _skip_row(pr, "code-owed")
            for pr in range(1, _OWED_ACT_DIVERGENCE_DATA_FLOOR + 1)
        ]
        summary = format_known_state_summary(
            gate_states={},
            recent_goals=[],
            gate_activation_backend=_OwedActGateBackend(rows),
        )
        assert "OWED-ACT-DIVERGENCE:" in summary

    def test_no_backend_omits_the_line(self) -> None:
        """Without a backend the block stays empty — the function stays pure."""
        summary = format_known_state_summary(gate_states={}, recent_goals=[])
        assert summary == ""

    def test_clean_trace_omits_the_line(self) -> None:
        """A backend with no divergence adds nothing to the block."""
        rows = [
            _skip_row(1, "wait-ci")
            for _ in range(_OWED_ACT_DIVERGENCE_DATA_FLOOR + 1)
        ]
        summary = format_known_state_summary(
            gate_states={},
            recent_goals=[],
            gate_activation_backend=_OwedActGateBackend(rows),
        )
        assert summary == ""
