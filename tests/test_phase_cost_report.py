"""Tests for maestro.phase_cost_report."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from maestro.cost_tracking import CALLER_GROUPS, CostSample, JsonlCostSampleStore
from maestro.phase_cost_report import (
    PhaseAggregate,
    _distribute_to_phases,
    _format_table,
    _reduction_target,
    generate_phase_cost_report,
    refresh_phase_cost_report,
)
from maestro.state_store import SqliteCostSampleStore, StateStore


def _sample(
    *,
    caller: str = "introspect",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    ts: str = "2026-01-01T00:00:00+00:00",
) -> CostSample:
    """Build a fully-specified :class:`CostSample` for the phase-report tests."""
    return CostSample(
        timestamp=ts,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        caller=caller,
        duration_ms=0,
    )


def _write_samples(
    mem_fs: dict[str, str], path: str, samples: list[CostSample],
) -> None:
    """Serialise *samples* as a ``cost_samples.jsonl`` file into ``mem_fs``."""
    mem_fs[path] = "\n".join(json.dumps(asdict(s)) for s in samples) + "\n"


# ---- _distribute_to_phases ----


class TestDistributeToPhases:
    def test_retired_doc_proposal_callers_keep_their_phase(self) -> None:
        """Proposal 149 Step 4: historical doc-proposal spend is not "Other"."""
        phases = _distribute_to_phases([
            _sample(caller="doc_proposals", cost_usd=0.5),
            _sample(caller="doc_proposals_session", cost_usd=0.25),
        ])
        assert "Other" not in phases
        assert phases["Artifacts & Docs"].calls == 2
        assert phases["Artifacts & Docs"].cost_usd == 0.75

    def test_formerly_unmapped_callers_do_not_land_in_other(self) -> None:
        """Regression #4135: curation/validation/replan spend has a real phase."""
        phases = _distribute_to_phases([
            _sample(caller="curate_pending_plans", cost_usd=0.5),
            _sample(caller="dream_validate", cost_usd=0.2),
            _sample(caller="replan_for_scope", cost_usd=0.1),
        ])
        assert "Other" not in phases
        assert phases["Evolution"].calls == 1
        assert phases["Dreaming"].calls == 1
        assert phases["Implementation"].calls == 1

    def test_single_caller_single_sample(self) -> None:
        phases = _distribute_to_phases([
            _sample(caller="introspect", input_tokens=1000, output_tokens=200,
                    cost_usd=0.10),
        ])
        assert "Contemplation" in phases
        agg = phases["Contemplation"]
        assert agg.input_tokens == 1000
        assert agg.output_tokens == 200
        assert abs(agg.cost_usd - 0.10) < 1e-9
        assert agg.calls == 1

    def test_token_weighted_attribution(self) -> None:
        """Heavy implement_plan call must dominate light introspect call.

        This is the regression test for the defect: previously the report
        weighted shares by call count, splitting a 50k-token ``implement_plan``
        call and a 500-token ``introspect`` call 50/50.  With per-call
        sample sourcing, attribution is exact.
        """
        phases = _distribute_to_phases([
            _sample(caller="implement_plan", input_tokens=50_000,
                    output_tokens=5_000, cost_usd=1.00),
            _sample(caller="introspect", input_tokens=500, output_tokens=50,
                    cost_usd=0.01),
        ])
        impl = phases["Implementation"].cost_usd
        contemp = phases["Contemplation"].cost_usd
        assert impl > 5 * contemp, (
            f"Expected Implementation >> Contemplation (real cost split is ~100:1 "
            f"by token use), but got {impl:.4f} vs {contemp:.4f}. Phase attribution "
            f"must be weighted by per-call cost, not call count."
        )
        assert abs(impl - 1.00) < 1e-9
        assert abs(contemp - 0.01) < 1e-9

    def test_two_callers_separate_samples(self) -> None:
        phases = _distribute_to_phases([
            _sample(caller="introspect", input_tokens=300, output_tokens=100,
                    cost_usd=0.30),
            _sample(caller="execute_step", input_tokens=600, output_tokens=200,
                    cost_usd=0.60),
        ])
        assert phases["Contemplation"].input_tokens == 300
        assert phases["Contemplation"].output_tokens == 100
        assert abs(phases["Contemplation"].cost_usd - 0.30) < 1e-9
        assert phases["Planning & Execution"].input_tokens == 600
        assert phases["Planning & Execution"].output_tokens == 200
        assert abs(phases["Planning & Execution"].cost_usd - 0.60) < 1e-9

    def test_unknown_caller_goes_to_other(self) -> None:
        """A caller absent from CALLER_GROUPS lands in the ``Other`` bucket."""
        phases = _distribute_to_phases([
            _sample(caller="alien_caller", input_tokens=100, output_tokens=50,
                    cost_usd=0.05),
        ])
        assert "Other" in phases
        assert phases["Other"].input_tokens == 100
        assert phases["Other"].calls == 1

    def test_multiple_samples_accumulate(self) -> None:
        phases = _distribute_to_phases([
            _sample(caller="introspect", input_tokens=100, output_tokens=10,
                    cost_usd=0.05),
            _sample(caller="introspect", input_tokens=200, output_tokens=20,
                    cost_usd=0.10),
        ])
        assert phases["Contemplation"].input_tokens == 300
        assert phases["Contemplation"].output_tokens == 30
        assert abs(phases["Contemplation"].cost_usd - 0.15) < 1e-9
        assert phases["Contemplation"].calls == 2

    def test_mixed_known_unknown_callers(self) -> None:
        """Each sample is attributed independently by its caller label."""
        phases = _distribute_to_phases([
            _sample(caller="introspect", input_tokens=100, output_tokens=10,
                    cost_usd=0.10),
            _sample(caller="dream", input_tokens=100, output_tokens=10,
                    cost_usd=0.10),
            _sample(caller="unknown_x", input_tokens=200, output_tokens=20,
                    cost_usd=0.20),
        ])
        assert phases["Contemplation"].input_tokens == 100
        assert phases["Dreaming"].input_tokens == 100
        assert phases["Other"].input_tokens == 200

    def test_empty_samples_returns_empty_dict(self) -> None:
        phases = _distribute_to_phases([])
        assert phases == {}


# ---- _reduction_target ----


class TestReductionTarget:
    def test_top_3_get_target(self) -> None:
        assert _reduction_target("Implementation", 1) != ""
        assert _reduction_target("Evolution", 2) != ""
        assert _reduction_target("Contemplation", 3) != ""

    def test_rank_4_empty(self) -> None:
        assert _reduction_target("Implementation", 4) == ""

    def test_rank_5_empty(self) -> None:
        assert _reduction_target("Dreaming", 5) == ""

    def test_unknown_phase(self) -> None:
        assert _reduction_target("UnknownPhase", 1) == "lower effort level"

    def test_known_phase_targets(self) -> None:
        """Each known phase returns its specific target string."""
        assert _reduction_target("Contemplation", 1) == "increase cadence interval"
        assert _reduction_target("Evolution", 1) == "reduce context size"
        assert _reduction_target("Implementation", 1) == "reduce context size"
        assert _reduction_target("Purpose", 1) == "lower effort level"
        assert _reduction_target("Planning & Execution", 1) == "reduce context size"
        assert _reduction_target("Dreaming", 1) == "increase cadence interval"
        assert _reduction_target("Task Generation", 1) == "increase cadence interval"
        assert _reduction_target("Probes", 1) == "lower effort level"
        assert _reduction_target("Artifacts & Docs", 1) == "increase cadence interval"
        assert _reduction_target("Other", 1) == "lower effort level"

    def test_every_caller_group_has_a_target(self) -> None:
        """A new CALLER_GROUPS group must not silently fall back to the default."""
        from maestro.phase_cost_report import _PHASE_REDUCTION_TARGETS

        assert set(CALLER_GROUPS.values()) | {"Other"} == set(_PHASE_REDUCTION_TARGETS)


# ---- _format_table ----


class TestFormatTable:
    def test_renders_header(self) -> None:
        phases = {
            "Contemplation": PhaseAggregate(
                phase="Contemplation",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.10,
                calls=2,
            ),
        }
        md = _format_table(phases)
        assert "| Phase |" in md
        assert "| Contemplation |" in md

    def test_renders_calls_column(self) -> None:
        """The accumulated per-phase call count is rendered, not dropped."""
        phases = {
            "Contemplation": PhaseAggregate(
                phase="Contemplation",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.10,
                calls=7,
            ),
        }
        md = _format_table(phases)
        assert "| Calls |" in md
        data_lines = [
            ln for ln in md.split("\n")
            if ln.startswith("| ") and "Phase" not in ln and "---" not in ln
        ]
        assert "| 7 |" in data_lines[0]

    def test_ranks_descending(self) -> None:
        phases = {
            "Low": PhaseAggregate(phase="Low", cost_usd=0.01),
            "High": PhaseAggregate(phase="High", cost_usd=0.50),
            "Mid": PhaseAggregate(phase="Mid", cost_usd=0.10),
        }
        md = _format_table(phases)
        lines = md.split("\n")
        data_lines = [
            ln for ln in lines
            if ln.startswith("| ") and "Phase" not in ln and "---" not in ln
        ]
        assert "High" in data_lines[0]
        assert "Mid" in data_lines[1]
        assert "Low" in data_lines[2]

    def test_percent_of_total(self) -> None:
        phases = {
            "A": PhaseAggregate(phase="A", cost_usd=0.75),
            "B": PhaseAggregate(phase="B", cost_usd=0.25),
        }
        md = _format_table(phases)
        assert "75.0%" in md
        assert "25.0%" in md

    def test_top_3_section(self) -> None:
        phases = {
            "A": PhaseAggregate(phase="A", cost_usd=0.50),
            "B": PhaseAggregate(phase="B", cost_usd=0.30),
            "C": PhaseAggregate(phase="C", cost_usd=0.10),
            "D": PhaseAggregate(phase="D", cost_usd=0.01),
        }
        md = _format_table(phases)
        assert "## Top-3 Reduction Targets" in md
        # Only first 3 listed in the targets section
        targets_section = md.split("## Top-3 Reduction Targets")[1]
        assert "**A**" in targets_section
        assert "**B**" in targets_section
        assert "**C**" in targets_section
        assert "**D**" not in targets_section

    def test_total_line(self) -> None:
        phases = {
            "A": PhaseAggregate(phase="A", cost_usd=0.10),
            "B": PhaseAggregate(phase="B", cost_usd=0.05),
        }
        md = _format_table(phases)
        assert "**Total across all phases:** $0.1500" in md

    def test_zero_cost_no_division_error(self) -> None:
        phases = {
            "A": PhaseAggregate(phase="A", cost_usd=0.0),
        }
        md = _format_table(phases)
        assert "0.0%" in md

    def test_empty_phases(self) -> None:
        """Empty dict produces header only, no top-3 section."""
        md = _format_table({})
        assert "# Phase Cost Report" in md
        assert "**Total across all phases:** $0.0000" in md
        assert "## Top-3 Reduction Targets" not in md

    def test_single_phase_formatting(self) -> None:
        """Single phase produces 100% share and is top-1."""
        phases = {
            "Dreaming": PhaseAggregate(
                phase="Dreaming",
                input_tokens=500,
                output_tokens=50,
                cost_usd=0.03,
                calls=1,
            ),
        }
        md = _format_table(phases)
        assert "100.0%" in md
        assert "**Total across all phases:** $0.0300" in md
        assert "1. **Dreaming**" in md

    def test_token_formatting_with_commas(self) -> None:
        """Large token counts get comma formatting."""
        phases = {
            "A": PhaseAggregate(
                phase="A",
                input_tokens=1_234_567,
                output_tokens=89_012,
                cost_usd=1.50,
                calls=10,
            ),
        }
        md = _format_table(phases)
        assert "1,234,567" in md
        assert "89,012" in md


# ---- generate_phase_cost_report (end-to-end, JSONL backend) ----


class TestGeneratePhaseReport:
    def test_empty_file(self, mem_fs: dict[str, str]) -> None:
        backend = JsonlCostSampleStore(Path("/tmp/cost_samples.jsonl"))
        content = generate_phase_cost_report(
            backend, output_path=Path("/tmp/out/report.md"),
        )
        assert "No usage records found" in content
        assert "No usage records found" in mem_fs["/tmp/out/report.md"]

    def test_missing_file(self, mem_fs: dict[str, str]) -> None:
        """Non-existent samples file produces 'no records' report."""
        backend = JsonlCostSampleStore(Path("/tmp/nonexistent.jsonl"))
        content = generate_phase_cost_report(
            backend, output_path=Path("/tmp/out/report.md"),
        )
        assert "No usage records found" in content
        assert mem_fs["/tmp/out/report.md"] == content

    def test_end_to_end(self, mem_fs: dict[str, str]) -> None:
        _write_samples(mem_fs, "/tmp/cost_samples.jsonl", [
            _sample(caller="introspect", input_tokens=1000, output_tokens=200,
                    cost_usd=0.10, ts="2026-01-01T00:00:00+00:00"),
            _sample(caller="execute_step", input_tokens=2000, output_tokens=400,
                    cost_usd=0.20, ts="2026-01-01T00:01:00+00:00"),
            _sample(caller="dream", input_tokens=500, output_tokens=100,
                    cost_usd=0.05, ts="2026-01-01T01:00:00+00:00"),
        ])
        content = generate_phase_cost_report(
            JsonlCostSampleStore(Path("/tmp/cost_samples.jsonl")),
            output_path=Path("/tmp/out/report.md"),
            n_recent=200,
        )
        assert "# Phase Cost Report" in content
        assert "Contemplation" in content
        assert "Planning & Execution" in content
        assert "Dreaming" in content
        assert "## Top-3 Reduction Targets" in content
        # Verify file was written
        assert content == mem_fs["/tmp/out/report.md"]

    def test_n_recent_respected(self, mem_fs: dict[str, str]) -> None:
        _write_samples(mem_fs, "/tmp/cost_samples.jsonl", [
            _sample(caller="introspect", input_tokens=100, output_tokens=10,
                    cost_usd=0.01)
            for _ in range(10)
        ])
        content = generate_phase_cost_report(
            JsonlCostSampleStore(Path("/tmp/cost_samples.jsonl")),
            output_path=Path("/tmp/out/report.md"),
            n_recent=3,
        )
        # Only the last 3 samples -> 300 input tokens total for Contemplation
        assert "300" in content

    def test_single_phase_report(self, mem_fs: dict[str, str]) -> None:
        """File with one sample -> one phase in the report."""
        _write_samples(mem_fs, "/tmp/cost_samples.jsonl", [
            _sample(caller="dream", input_tokens=500, output_tokens=50,
                    cost_usd=0.05),
        ])
        content = generate_phase_cost_report(
            JsonlCostSampleStore(Path("/tmp/cost_samples.jsonl")),
            output_path=Path("/tmp/out/report.md"),
        )
        assert "Dreaming" in content
        assert "100.0%" in content
        assert "Contemplation" not in content

    def test_malformed_lines_skipped_end_to_end(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Malformed lines in the file are skipped; valid ones processed."""
        valid = json.dumps(asdict(
            _sample(caller="introspect", input_tokens=100, output_tokens=10,
                    cost_usd=0.01),
        ))
        mem_fs["/tmp/cost_samples.jsonl"] = (
            "not json at all\n"
            f"{valid}\n"
            "{bad json\n"
        )
        content = generate_phase_cost_report(
            JsonlCostSampleStore(Path("/tmp/cost_samples.jsonl")),
            output_path=Path("/tmp/out/report.md"),
        )
        assert "Contemplation" in content
        assert "100" in content  # 100 input tokens

    def test_all_malformed_lines(self, mem_fs: dict[str, str]) -> None:
        """File with only malformed lines -> 'no records' report."""
        mem_fs["/tmp/cost_samples.jsonl"] = (
            "not json\n"
            "{bad\n"
            "also bad\n"
        )
        content = generate_phase_cost_report(
            JsonlCostSampleStore(Path("/tmp/cost_samples.jsonl")),
            output_path=Path("/tmp/out/report.md"),
        )
        assert "No usage records found" in content

    def test_top_3_ranking_reflects_per_call_cost(
        self, mem_fs: dict[str, str],
    ) -> None:
        """End-to-end: the headline Top-3 ranking reflects exact per-call cost.

        Two cheap ``introspect`` calls and one expensive ``implement_plan``
        call: Implementation must outrank Contemplation in the Top-3
        section even though Contemplation has more calls.
        """
        _write_samples(mem_fs, "/tmp/cost_samples.jsonl", [
            _sample(caller="introspect", input_tokens=500, output_tokens=50,
                    cost_usd=0.01),
            _sample(caller="introspect", input_tokens=500, output_tokens=50,
                    cost_usd=0.01),
            _sample(caller="implement_plan", input_tokens=50_000,
                    output_tokens=5_000, cost_usd=1.00),
        ])
        content = generate_phase_cost_report(
            JsonlCostSampleStore(Path("/tmp/cost_samples.jsonl")),
            output_path=Path("/tmp/out/report.md"),
        )
        top_section = content.split("## Top-3 Reduction Targets")[1]
        impl_pos = top_section.find("**Implementation**")
        contemp_pos = top_section.find("**Contemplation**")
        assert impl_pos >= 0
        assert contemp_pos >= 0
        assert impl_pos < contemp_pos

    def test_end_to_end_reads_from_sqlite_backend(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The report reads the recent-N samples from a SQLite backend too.

        Closes the coherence gap: with ``[paths] sqlite_state_enabled`` on the
        JSONL is never written, so the dream-cycle report must read ``state.db``
        via ``SqliteCostSampleStore.recent_samples`` — an in-memory store proves
        the same aggregation lands from that substrate. ``mem_fs`` backs only the
        Markdown output file; the samples live in ``:memory:``.
        """
        backend = SqliteCostSampleStore(StateStore(":memory:"))
        for s in (
            _sample(caller="introspect", input_tokens=500, output_tokens=50,
                    cost_usd=0.01),
            _sample(caller="implement_plan", input_tokens=50_000,
                    output_tokens=5_000, cost_usd=1.00),
        ):
            backend.append(s)
        content = generate_phase_cost_report(
            backend, output_path=Path("/tmp/out/report.md"),
        )
        assert "Implementation" in content
        assert "Contemplation" in content
        assert content == mem_fs["/tmp/out/report.md"]


# ---- refresh_phase_cost_report ----


class TestRefreshPhaseCostReport:
    """The dream phase's best-effort refresh (proposal 149 Step 5)."""

    def test_writes_report(self, mem_fs: dict[str, str]) -> None:
        samples_path = "/fake/refresh/cost_samples.jsonl"
        _write_samples(mem_fs, samples_path, [_sample(caller="reflect", cost_usd=0.5)])
        out = Path("/fake/refresh/artifacts/phase_cost_report.md")
        refresh_phase_cost_report(JsonlCostSampleStore(Path(samples_path)), out)
        assert "# Phase Cost Report" in mem_fs[str(out)]

    def test_skips_without_backend(self, mem_fs: dict[str, str]) -> None:
        out = Path("/fake/refresh/artifacts/phase_cost_report.md")
        with patch("maestro.phase_cost_report.generate_phase_cost_report") as gen:
            refresh_phase_cost_report(None, out)
        gen.assert_not_called()
        assert str(out) not in mem_fs

    def test_failure_is_logged_and_swallowed(
        self, caplog: Any,
    ) -> None:
        out = Path("/fake/refresh/artifacts/phase_cost_report.md")
        with patch(
            "maestro.phase_cost_report.generate_phase_cost_report",
            side_effect=OSError("disk full"),
        ), caplog.at_level(logging.WARNING, logger="maestro"):
            refresh_phase_cost_report(MagicMock(), out)
        assert "Phase cost report refresh failed" in caplog.text
