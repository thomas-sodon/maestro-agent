"""The ``measure-*`` CLI subcommands and pr-laps; split out of
``tests/test_cli.py``.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

from maestro.cli import (
    cmd_audit_flags,
    cmd_list_gates,
    cmd_measure_confirmed_landings,
    cmd_measure_dead_on_arrival_mints,
    cmd_measure_false_success,
    cmd_measure_gate_contention,
    cmd_measure_gate_triggers,
    cmd_measure_handoff_relapse,
    cmd_measure_noops,
    cmd_measure_outcome_trends,
    cmd_measure_phantom_landings,
    cmd_measure_plan_mill,
    cmd_measure_premise_accuracy,
    cmd_measure_rejection_classes,
    cmd_measure_relap,
    cmd_pr_laps,
)
from maestro.config import Config
from maestro.memory import (
    EvolutionPlan,
    Memory,
    TaskRecord,
)
from maestro.messaging import Message, MessageStore
from maestro.types import TaskOutcome, TaskSource
from tests.conftest import capture_stdout
from tests.test_cli import (
    _doa_cli_plan,
    _make_config,
    _rejection,
)

# ── cmd_measure_noops ──────────────────────────────────────────────


def _make_noop_task(
    task_id: str = "t1",
    outcome: str = "success",
    result_summary: str = "Done",
    source: str = "external",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal="some goal",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary=result_summary,
        errors=[],
        duration_secs=60.0,
        timestamp="2026-05-26T10:00:00",
        source=source,  # type: ignore[arg-type]
    )


def test_cmd_measure_noops_empty(mem_fs: dict[str, str]) -> None:
    """Empty task history prints a zero report."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_noops, args, config)
    assert "No-op Report" in output
    assert "Successful tasks: 0" in output
    assert "No-op tasks:      0" in output
    assert "No-op rate:       0.0%" in output
    assert "No-op task IDs:" not in output


def test_cmd_measure_noops_mixed_tasks(mem_fs: dict[str, str]) -> None:
    """Mixed no-op and meaningful tasks produce correct summary."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", result_summary="No remediation needed",
        source="external",
    ))
    memory.record_task(_make_noop_task(
        task_id="t2", result_summary="Shipped new feature",
        source="self_directed",
    ))
    memory.record_task(_make_noop_task(
        task_id="t3", result_summary="Already passing all tests",
        source="external",
    ))
    memory.record_task(_make_noop_task(
        task_id="t4", outcome="failure",
        result_summary="No remediation needed",
    ))

    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_noops, args, config)

    # Overall stats: 3 successful tasks (t4 is failure), 2 no-ops (t1, t3)
    assert "4 tasks examined" in output
    assert "Successful tasks: 3" in output
    assert "No-op tasks:      2" in output
    assert "66.7%" in output  # 2/3

    # Per-source breakdown
    assert "By source:" in output
    assert "external" in output
    assert "self_directed" in output

    # No-op task IDs listed
    assert "No-op task IDs:" in output
    assert "t1" in output
    assert "t3" in output
    # t2 is not a no-op, t4 is failure
    assert "  - t4" not in output


def test_cmd_measure_noops_last_flag(mem_fs: dict[str, str]) -> None:
    """The --last flag limits to recent N tasks."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    for i in range(10):
        memory.record_task(_make_noop_task(
            task_id=f"t{i}",
            result_summary="No remediation needed" if i % 2 == 0 else "Shipped",
        ))

    args = argparse.Namespace(last=4)
    output = capture_stdout(cmd_measure_noops, args, config)
    assert "4 tasks examined" in output


def test_cmd_measure_noops_no_noop_ids_section_when_none(
    mem_fs: dict[str, str],
) -> None:
    """When no tasks are no-op, the 'No-op task IDs' section is omitted."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", result_summary="Shipped a real feature",
    ))
    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_noops, args, config)
    assert "No-op tasks:      0" in output
    assert "No-op task IDs:" not in output


def test_cmd_measure_noops_output_format(mem_fs: dict[str, str]) -> None:
    """Verify the report has the expected structural elements."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", result_summary="Already green",
    ))
    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_noops, args, config)

    # Structural elements
    assert "No-op Report" in output
    assert "=" * 40 in output
    assert "Successful tasks:" in output
    assert "No-op tasks:" in output
    assert "No-op rate:" in output
    assert "No-op task IDs:" in output
    assert "  - t1" in output


def test_cmd_measure_noops_last_none_reads_history(
    mem_fs: dict[str, str],
) -> None:
    """When last=None, history is examined (not zero).

    This guards against the regression where ``else 0`` caused
    ``get_recent_tasks(n=0)`` to return ``[]`` when ``--last`` is omitted,
    so the report always claimed ``0 tasks examined`` despite the help
    text promising the default is 'all'.
    """
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", result_summary="Already green",
    ))
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_noops, args, config)
    # With last=None the seeded task must be read, not skipped as n=0.
    assert "1 tasks examined" in output


def test_cmd_measure_noops_last_zero(mem_fs: dict[str, str]) -> None:
    """--last=0 is treated as 'all' (same as None), reading history."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", result_summary="Already green",
    ))
    args = argparse.Namespace(last=0)
    output = capture_stdout(cmd_measure_noops, args, config)
    # last=0 → treated as 'all' → the seeded task is examined.
    assert "1 tasks examined" in output


def test_cmd_measure_noops_last_negative(mem_fs: dict[str, str]) -> None:
    """Negative --last is treated as 'all', reading history."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", result_summary="Already green",
    ))
    args = argparse.Namespace(last=-5)
    output = capture_stdout(cmd_measure_noops, args, config)
    # Negative last → treated as 'all' → the seeded task is examined.
    assert "1 tasks examined" in output


def test_cmd_measure_noops_per_source_format(mem_fs: dict[str, str]) -> None:
    """Per-source breakdown lines follow 'source: noop/total (rate%)' format."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", result_summary="No remediation needed",
        source="pending_pr_requests",
    ))
    memory.record_task(_make_noop_task(
        task_id="t2", result_summary="Shipped feature",
        source="pending_pr_requests",
    ))
    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_noops, args, config)
    assert "pending_pr_requests: 1/2 (50.0%)" in output


def test_cmd_measure_noops_missing_by_source_section_when_empty(
    mem_fs: dict[str, str],
) -> None:
    """When all tasks are failures, no by-source section is printed."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1", outcome="failure",
        result_summary="No remediation needed",
    ))
    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_noops, args, config)
    assert "By source:" not in output


# ---------------------------------------------------------------------------
# cmd_measure_gate_triggers
# ---------------------------------------------------------------------------


def test_cmd_measure_gate_triggers_empty(mem_fs: dict[str, str]) -> None:
    """No gate_activations.jsonl prints 'No gate activations' message."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_gate_triggers, args, config)
    assert "No gate activations recorded" in output


def test_cmd_measure_gate_triggers_with_data(mem_fs: dict[str, str]) -> None:
    """Populated gate_activations.jsonl prints sorted report."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "cost_risk", "action": "blocked", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_gate_triggers, args, config)
    assert "Gate Activation Report" in output
    assert "audit_stall: 2" in output
    assert "cost_risk: 1" in output


def test_cmd_measure_gate_triggers_with_last(mem_fs: dict[str, str]) -> None:
    """The --last flag limits the window size."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "old_gate", "action": "a", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "new_gate", "action": "a", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=1)
    output = capture_stdout(cmd_measure_gate_triggers, args, config)
    assert "new_gate: 1" in output
    assert "old_gate" not in output


# ---------------------------------------------------------------------------
# cmd_measure_premise_accuracy (issue #2304)
# ---------------------------------------------------------------------------


def test_measure_premise_accuracy_subcommand_registered(
    mem_fs: dict[str, str],
) -> None:
    """main() routes 'measure-premise-accuracy' to its handler.

    Reaching the handler at all proves argparse registered the subcommand —
    an unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-premise-accuracy"]), \
         patch("maestro.cli.cmd_measure_premise_accuracy") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_measure_premise_accuracy_empty(mem_fs: dict[str, str]) -> None:
    """No premise_observations.jsonl prints the 'nothing to measure' message."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_premise_accuracy, args, config)
    assert "No premise observations to measure" in output


def test_cmd_measure_premise_accuracy_with_data(mem_fs: dict[str, str]) -> None:
    """Populated trace prints the accuracy summary and recent miss IDs."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "premise_observations.jsonl")
    mem_fs[path] = (
        '{"task_id": "miss1", "referenced": 1, "missing": 1, "timestamp": "t"}\n'
        '{"task_id": "hit1", "referenced": 2, "missing": 0, "timestamp": "t"}\n'
        '{"task_id": "none1", "referenced": 0, "missing": 0, "timestamp": "t"}\n'
    )
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_premise_accuracy, args, config)
    assert "Premise Accuracy Report" in output
    assert "Measured tasks:   2" in output
    assert "Premise hits:     1" in output
    assert "Premise accuracy: 50.0%" in output
    assert "Recent premise-miss task IDs:" in output
    assert "- miss1" in output
    # A hit / no-premise row never appears in the miss list.
    assert "hit1" not in output
    assert "none1" not in output


def test_cmd_measure_premise_accuracy_no_miss_section_when_all_hits(
    mem_fs: dict[str, str],
) -> None:
    """With no misses the recent-miss section is omitted."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "premise_observations.jsonl")
    mem_fs[path] = (
        '{"task_id": "h1", "referenced": 1, "missing": 0, "timestamp": "t"}\n'
    )
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_premise_accuracy, args, config)
    assert "Premise accuracy: 100.0%" in output
    assert "Recent premise-miss task IDs:" not in output


def test_cmd_measure_premise_accuracy_with_last(mem_fs: dict[str, str]) -> None:
    """The --last flag limits the window size."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "premise_observations.jsonl")
    mem_fs[path] = (
        '{"task_id": "old", "referenced": 1, "missing": 0, "timestamp": "t"}\n'
        '{"task_id": "new", "referenced": 1, "missing": 1, "timestamp": "t"}\n'
    )
    args = argparse.Namespace(last=1)
    output = capture_stdout(cmd_measure_premise_accuracy, args, config)
    # Only the newest row is in the window: one measured, zero hits.
    assert "Measured tasks:   1" in output
    assert "Premise accuracy: 0.0%" in output
    assert "- new" in output
    assert "old" not in output


# ---------------------------------------------------------------------------
# cmd_audit_flags (proposal 073)
# ---------------------------------------------------------------------------


def test_cmd_audit_flags_lists_every_manifest_flag(mem_fs: dict[str, str]) -> None:
    """Each seeded manifest flag and its review_by appear in the human table."""
    from maestro.flag_lifecycle import EXPERIMENTAL_FLAGS

    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_audit_flags, args, config)
    for entry in EXPERIMENTAL_FLAGS:
        assert entry.flag in output
        assert entry.review_by in output


def test_cmd_audit_flags_surfaces_gate_fire_rate(mem_fs: dict[str, str]) -> None:
    """A gate-linked flag's line shows the count of its gate's activations."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "phantom_escalation_delivery", "action": "a", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "phantom_escalation_delivery", "action": "a", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_audit_flags, args, config)
    line = next(
        ln for ln in output.splitlines()
        if "phantom_escalation_delivery_enabled" in ln
    )
    assert "activations 2" in line


def test_cmd_audit_flags_json_output(mem_fs: dict[str, str]) -> None:
    """--json emits an object with experimental and cut-over row lists.

    The payload is ``{"experimental_flags": [...], "cutover_flags": [...]}`` so both
    advisory reports are reachable via the read surface, not just the human table.
    """
    from maestro.flag_lifecycle import EXPERIMENTAL_FLAGS

    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "phantom_escalation_delivery", "action": "a", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_audit_flags, args, config)
    data = json.loads(output)
    assert isinstance(data, dict)
    assert set(data) == {"experimental_flags", "cutover_flags"}

    exp = data["experimental_flags"]
    assert isinstance(exp, list)
    assert len(exp) == len(EXPERIMENTAL_FLAGS)
    by_flag = {row["flag"]: row for row in exp}
    assert by_flag["phantom_escalation_delivery_enabled"]["activations"] == 1
    # A gate-linked flag that never fired carries 0, not null (r1) — a zero
    # fire-rate stays a live promote-or-delete signal.
    assert by_flag["unconfirmed_landing_forced_enabled"]["activations"] == 0
    # A no-trace (gate=None) flag carries a null activation count.
    assert by_flag["owned_act_preempt_enabled"]["activations"] is None
    # The manifest note is reachable via the --json read surface (r3).
    by_manifest = {entry.flag: entry for entry in EXPERIMENTAL_FLAGS}
    for row in exp:
        assert row["note"] == by_manifest[row["flag"]].note

    # The cut-over report rides the same object: the six cutover-stage manifest
    # entries, all unmeasured (gate=None) so each carries a null divergence count
    # and is not recommended. Proposal 111 collapsed live_work_corpus_enabled and
    # dropped its entry; the proposal-122 account_ui_v2 graduation added one, and
    # the proposal-128 tend_main_merge_enabled kill-switch added another.
    cutover = data["cutover_flags"]
    assert isinstance(cutover, list)
    cutover_by_flag = {row["flag"]: row for row in cutover}
    expected_cutover = {
        "observed_ready_standdown_enabled",
        "owned_committed_push_actuator_enabled",
        "account_ui_v2",
        "tend_main_merge_enabled",
        "conscious_workspace_observe_enabled",
        "generation_admission_enabled",
        "retire_unlanded_limitations_enabled",
    }
    assert set(cutover_by_flag) == expected_cutover
    for flag in expected_cutover:
        assert cutover_by_flag[flag]["divergences"] is None
        assert cutover_by_flag[flag]["recommend_collapse_eligible"] is False


def test_cmd_audit_flags_zero_fire_gate_flag_renders_zero(
    mem_fs: dict[str, str],
) -> None:
    """A gate-linked flag that never fired renders '0', not '-' (r1).

    ``measure_gate_triggers`` omits zero-count gates, so without a 0-default a
    gate-linked flag with no activations would collapse to None/'-',
    indistinguishable from a no-trace (gate=None) flag and erasing the
    promote-or-delete fire-rate signal.
    """
    config = _make_config()
    config.ensure_dirs()
    # Only one gate-linked flag's gate fires; the others fire 0x.
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "phantom_escalation_delivery", "action": "a", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_audit_flags, args, config)
    # A different gate-linked flag with zero fires renders '0'.
    zero_line = next(
        ln for ln in output.splitlines()
        if "unconfirmed_landing_forced_enabled" in ln
    )
    assert "activations 0" in zero_line
    # The no-trace (gate=None) flag still renders '-'.
    none_line = next(
        ln for ln in output.splitlines()
        if "owned_act_preempt_enabled" in ln
    )
    assert "activations -" in none_line


def test_cmd_audit_flags_with_last(mem_fs: dict[str, str]) -> None:
    """--last <N> limits the activation window before the manifest join (r2).

    Mirrors :func:`test_cmd_measure_gate_triggers_with_last`: only the newest N
    records feed the fire-rate count, so an older activation outside the window
    is not counted against its flag.
    """
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "phantom_escalation_delivery", "action": "a", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "unconfirmed_landing_forced", "action": "a", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=1, json=False)
    output = capture_stdout(cmd_audit_flags, args, config)
    # Only the newest record (unconfirmed_landing_forced) is in the window.
    forced_line = next(
        ln for ln in output.splitlines()
        if "unconfirmed_landing_forced_enabled" in ln
    )
    assert "activations 1" in forced_line
    # The older phantom_escalation_delivery record falls outside --last 1.
    phantom_line = next(
        ln for ln in output.splitlines()
        if "phantom_escalation_delivery_enabled" in ln
    )
    assert "activations 0" in phantom_line


def test_cmd_audit_flags_no_trace_flag_renders_dash(
    mem_fs: dict[str, str],
) -> None:
    """With no gate_activations.jsonl, a no-trace flag's line renders '-'."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_audit_flags, args, config)
    line = next(
        ln for ln in output.splitlines()
        if "owned_act_preempt_enabled" in ln
    )
    assert "activations -" in line


def test_cmd_audit_flags_renders_overdue_marker(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crossed-horizon entry renders '[OVERDUE]' and carries overdue:true (json).

    The seeded manifest all reviews in 2027, so against the real clock the
    ``[OVERDUE]`` render branch and the ``overdue: True`` --json row are never
    exercised. coverage.py does not split the one-line ``[OVERDUE] if ... else``
    conditional into branches, so the 100% gate passes with the marker branch
    dead — a wrong marker or an inverted horizon would ship silently. Inject a
    fixed ``today`` past a past-dated manifest entry to drive it deterministically.
    """
    from datetime import date

    from maestro import flag_lifecycle

    class _FixedToday(date):
        @classmethod
        def today(cls) -> date:
            return date(2027, 1, 1)

    manifest = (
        flag_lifecycle.ExperimentalFlag(
            flag="owned_act_preempt_enabled",
            review_by="2026-06-30",
            gate=None,
            note="past-dated entry — crossed its horizon",
        ),
        flag_lifecycle.ExperimentalFlag(
            flag="session_landing_enabled",
            review_by="2027-12-31",
            gate=None,
            note="future entry — horizon not yet crossed",
        ),
    )
    monkeypatch.setattr(flag_lifecycle, "EXPERIMENTAL_FLAGS", manifest)
    monkeypatch.setattr("maestro.cli.date", _FixedToday)

    config = _make_config()
    config.ensure_dirs()

    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_audit_flags, args, config)
    overdue_line = next(
        ln for ln in output.splitlines()
        if "owned_act_preempt_enabled" in ln
    )
    assert "[OVERDUE]" in overdue_line
    future_line = next(
        ln for ln in output.splitlines()
        if "session_landing_enabled" in ln
    )
    assert "[OVERDUE]" not in future_line

    json_args = argparse.Namespace(last=None, json=True)
    json_output = capture_stdout(cmd_audit_flags, json_args, config)
    rows = {
        row["flag"]: row
        for row in json.loads(json_output)["experimental_flags"]
    }
    assert rows["owned_act_preempt_enabled"]["overdue"] is True
    assert rows["session_landing_enabled"]["overdue"] is False


def test_cmd_audit_flags_renders_cutover_divergences(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Cut-Over Burn-In section renders a cutover flag and its divergence count.

    The real manifest holds no ``cutover``-stage entry after proposal 111 (both
    survivors are ``retain``), so a gate-linked synthetic entry is monkeypatched in
    to exercise the divergence join: two seeded activations of its divergence gate
    render as ``divergences 2``. ``cmd_audit_flags`` re-imports ``CUTOVER_FLAGS``
    from the module at call time, so the patch is picked up (mirroring the
    ``EXPERIMENTAL_FLAGS`` patch above).
    """
    from maestro import flag_lifecycle

    manifest = (
        flag_lifecycle.CutoverFlag(
            flag="demo_cutover_flag",
            proposal=100,
            cutover_cycle="2026-01-01",
            off_path_symbol="demo_cutover_off_path",
            review_by="2027-12-31",
            stage="cutover",
            note="synthetic gate-linked cutover entry",
            gate="live_corpus_divergence",
        ),
    )
    monkeypatch.setattr(flag_lifecycle, "CUTOVER_FLAGS", manifest)

    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "live_corpus_divergence", "action": "a", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "live_corpus_divergence", "action": "a", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_audit_flags, args, config)
    # The section header names the same n-record window the experimental section
    # does (default 100), so a zero-divergence read is never mistaken for a burn-in
    # measured over the flag's whole age (r2).
    assert "Cut-Over Burn-In (divergences over last 100 records)" in output
    line = next(
        ln for ln in output.splitlines()
        if "demo_cutover_flag" in ln
    )
    assert "divergences 2" in line


def test_cmd_audit_flags_renders_cutover_recommendation(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A past-floor, zero-divergence, gate-linked cutover flag renders the recommend.

    The real manifest holds no ``cutover``-stage entry after proposal 111, so the
    ``[PAST FLOOR]`` and ``-> recommend collapse_eligible`` render branches are
    otherwise never exercised. Inject a fixed ``today`` past a past-dated,
    gate-linked cutover entry with zero seeded divergences to drive both markers
    deterministically; a below-floor variant asserts they stay absent.
    """
    from datetime import date

    from maestro import flag_lifecycle

    class _FixedToday(date):
        @classmethod
        def today(cls) -> date:
            return date(2027, 6, 1)

    def _cutover(review_by: str, note: str) -> tuple[flag_lifecycle.CutoverFlag, ...]:
        return (
            flag_lifecycle.CutoverFlag(
                flag="demo_cutover_flag",
                proposal=100,
                cutover_cycle="2026-01-01",
                off_path_symbol="demo_cutover_off_path",
                review_by=review_by,
                stage="cutover",
                note=note,
                gate="live_corpus_divergence",
            ),
        )

    monkeypatch.setattr("maestro.cli.date", _FixedToday)
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=False)

    # Aged past floor with zero seeded divergences -> recommended for collapse.
    monkeypatch.setattr(
        flag_lifecycle, "CUTOVER_FLAGS", _cutover("2027-01-01", "aged, clean"),
    )
    aged_output = capture_stdout(cmd_audit_flags, args, config)
    aged_line = next(
        ln for ln in aged_output.splitlines()
        if "demo_cutover_flag" in ln
    )
    assert "[PAST FLOOR]" in aged_line
    assert "-> recommend collapse_eligible" in aged_line

    # Below-floor variant: horizon not yet crossed -> no floor marker, no recommend.
    monkeypatch.setattr(
        flag_lifecycle, "CUTOVER_FLAGS", _cutover("2027-12-31", "below floor"),
    )
    below_output = capture_stdout(cmd_audit_flags, args, config)
    below_line = next(
        ln for ln in below_output.splitlines()
        if "demo_cutover_flag" in ln
    )
    assert "[PAST FLOOR]" not in below_line
    assert "-> recommend collapse_eligible" not in below_line


# ---------------------------------------------------------------------------
# cmd_measure_phantom_landings (proposal 105)
# ---------------------------------------------------------------------------


def test_measure_phantom_landings_subcommand_registered(
    mem_fs: dict[str, str],
) -> None:
    """main() routes 'measure-phantom-landings' to its handler.

    Reaching the handler at all proves argparse registered the subcommand —
    an unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-phantom-landings"]), \
         patch("maestro.cli.cmd_measure_phantom_landings") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_measure_phantom_landings_empty(mem_fs: dict[str, str]) -> None:
    """No shadow rows prints the all-zero report (0.0%)."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_phantom_landings, args, config)
    assert "Phantom-Landing Divergence Report" in output
    assert "Shadow records:     0" in output
    assert "Divergence rate:    0.0%" in output
    # Negative control: the empty report never claims a nonzero divergence.
    assert "100.0%" not in output


def test_cmd_measure_phantom_landings_with_data(
    mem_fs: dict[str, str],
) -> None:
    """Populated ledger prints exact counts and the divergence percentage."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "mechanical_outcome_authority", "action": "diverged", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "mechanical_outcome_authority", "action": "agree", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "mechanical_outcome_authority", "action": "agree", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "mechanical_outcome_authority", "action": "agree", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_phantom_landings, args, config)
    assert "Shadow records:     4" in output
    assert "Diverged:           1" in output
    assert "Agreed:             3" in output
    assert "Divergence rate:    25.0%" in output
    # Negative control: the unrelated audit_stall row is not counted.
    assert "Shadow records:     5" not in output


def test_cmd_measure_phantom_landings_with_last(
    mem_fs: dict[str, str],
) -> None:
    """The --last flag bounds the denominator to the last N shadow rows."""
    config = _make_config()
    config.ensure_dirs()
    path = str(config.memory_dir / "gate_activations.jsonl")
    mem_fs[path] = (
        '{"gate": "mechanical_outcome_authority", "action": "agree", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "mechanical_outcome_authority", "action": "agree", "timestamp": "t", "detail": "d"}\n'
        '{"gate": "mechanical_outcome_authority", "action": "diverged", "timestamp": "t", "detail": "d"}\n'
    )
    args = argparse.Namespace(last=1)
    output = capture_stdout(cmd_measure_phantom_landings, args, config)
    # Only the last shadow row (diverged) is in the window → 100.0%.
    assert "Shadow records:     1" in output
    assert "Divergence rate:    100.0%" in output
    # Negative control: without --last the rate would be 1/3 ≈ 33.3%.
    assert "33.3%" not in output


# ---------------------------------------------------------------------------
# cmd_measure_plan_mill (proposal 116, increment 1)
# ---------------------------------------------------------------------------


def _mill_cli_plan(
    plan_id: str,
    title: str = "Add a widget",
    rationale: str = "r",
    status: str = "pending_review",
    source_task_id: str | None = None,
) -> EvolutionPlan:
    """Build an EvolutionPlan carrying the fields the mill metrics read."""
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale=rationale,
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
        source_task_id=source_task_id,
    )


def _seed_mill_plans(config: Config) -> None:
    """Seed a mix: 2 themed, 2 merged, 1 coupled, 1 stranded-promotion of 3."""
    memory = Memory(config.memory_dir)
    memory.save_evolution_plan(_mill_cli_plan(
        "p1", title="Add a sensor", status="merged"))
    memory.save_evolution_plan(_mill_cli_plan(
        "p2", title="Add a widget", source_task_id="maestro/task/x"))
    memory.save_evolution_plan(_mill_cli_plan(
        "p3", title="Finish the stranded-branch sensor lineage",
        status="merged"))


def test_measure_plan_mill_subcommand_registered(
    mem_fs: dict[str, str],
) -> None:
    """main() routes 'measure-plan-mill' to its handler.

    Reaching the handler at all proves argparse registered the subcommand —
    an unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-plan-mill"]), \
         patch("maestro.cli.cmd_measure_plan_mill") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_measure_plan_mill_empty(mem_fs: dict[str, str]) -> None:
    """No plans prints the all-zero report rather than dividing by zero."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_plan_mill, args, config)
    assert "Plan-Mill Report (all created plans)" in output
    assert "Plans examined:     0" in output
    assert "Themed (self-meas): 0 (0.0%)" in output
    assert "Merged / created:   0/0 (0.0%)" in output
    assert "Task-coupled:       0 (0.0%)" in output
    assert "Stranded promos:    0" in output


def test_cmd_measure_plan_mill_human_output(mem_fs: dict[str, str]) -> None:
    """A seeded mix prints the exact metric lines."""
    config = _make_config()
    config.ensure_dirs()
    _seed_mill_plans(config)
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_plan_mill, args, config)
    assert "Plans examined:     3" in output
    assert "Themed (self-meas): 2 (66.7%)" in output
    assert "Merged / created:   2/3 (66.7%)" in output
    assert "Task-coupled:       1 (33.3%)" in output
    assert "Stranded promos:    1" in output
    # Value column alignment: every labelled report line lands its value at the
    # same column (guards the r3 regression where "Stranded promotions: " was
    # one char wider than the other four labels and shifted its value right).
    report_lines = [
        line for line in output.splitlines()
        if ":" in line and not line.startswith("Plan-Mill")
    ]
    assert len(report_lines) == 5
    value_columns = set()
    for line in report_lines:
        label, _, rest = line.partition(":")
        value_columns.add(len(label) + 1 + len(rest) - len(rest.lstrip(" ")))
    assert value_columns == {20}


def test_cmd_measure_plan_mill_json(mem_fs: dict[str, str]) -> None:
    """--json emits a single object with the four metric keys and values."""
    config = _make_config()
    config.ensure_dirs()
    _seed_mill_plans(config)
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_measure_plan_mill, args, config)
    data = json.loads(output)
    assert set(data) == {
        "theme_saturation", "merged_to_created",
        "task_coupled", "stranded_promotions",
    }
    assert data["theme_saturation"] == {
        "total": 3, "themed": 2, "themed_fraction": 2 / 3,
    }
    assert data["merged_to_created"] == {
        "created": 3, "merged": 2, "ratio": 2 / 3,
    }
    assert data["task_coupled"] == {
        "total": 3, "coupled": 1, "coupled_fraction": 1 / 3,
    }
    assert data["stranded_promotions"] == {"count": 1, "plan_ids": ["p3"]}


def test_cmd_measure_plan_mill_last_window(mem_fs: dict[str, str]) -> None:
    """--last=N labels the report and windows to the N most-recent plans.

    The seeded plans have no explicit created_at, so they window by insertion
    order; --last=2 keeps the last two (p2, p3), dropping p1. Exercises the
    n>0 branch and its 'last N' human-readable label through the handler.
    """
    config = _make_config()
    config.ensure_dirs()
    _seed_mill_plans(config)
    args = argparse.Namespace(last=2, json=False)
    output = capture_stdout(cmd_measure_plan_mill, args, config)
    assert "Plan-Mill Report (last 2 created plans)" in output
    assert "Plans examined:     2" in output
    assert "Themed (self-meas): 1 (50.0%)" in output
    assert "Merged / created:   1/2 (50.0%)" in output
    assert "Task-coupled:       1 (50.0%)" in output
    assert "Stranded promos:    1" in output


# ---------------------------------------------------------------------------
# cmd_measure_dead_on_arrival_mints (proposal 120)
# ---------------------------------------------------------------------------

# A persisted submission-door ``obsolete_reason`` signature, marking a plan
# obsoleted at the generation-admission gate (dead on arrival).
_DOA_CLI_REASON = (
    "Admission gate refused net-new mint: backlog saturated (5 >= 5) — land or "
    "dispose an existing plan before proposing more"
)


def _seed_doa_plans(config: Config) -> None:
    """Seed 3 plans: 1 door-obsoleted (evolve), 1 non-door obsolete, 1 merged."""
    memory = Memory(config.memory_dir)
    memory.save_evolution_plan(
        _doa_cli_plan("p1", obsolete_reason=_DOA_CLI_REASON))
    memory.save_evolution_plan(
        _doa_cli_plan("p2", obsolete_reason="superseded by a newer plan"))
    memory.save_evolution_plan(_doa_cli_plan("p3", status="merged"))


def test_measure_dead_on_arrival_mints_subcommand_registered(
    mem_fs: dict[str, str],
) -> None:
    """main() routes 'measure-dead-on-arrival-mints' to its handler.

    Reaching the handler at all proves argparse registered the subcommand —
    an unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-dead-on-arrival-mints"]), \
         patch("maestro.cli.cmd_measure_dead_on_arrival_mints") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_measure_dead_on_arrival_mints_empty(mem_fs: dict[str, str]) -> None:
    """No door-obsoleted plans prints the 'none' message with the window total."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_dead_on_arrival_mints, args, config)
    assert "No dead-on-arrival plan mints (window total: 0)." in output


def test_cmd_measure_dead_on_arrival_mints_human_output(
    mem_fs: dict[str, str],
) -> None:
    """A seeded mix prints the breakdown: 1 of 3 died at the door (evolve)."""
    config = _make_config()
    config.ensure_dirs()
    _seed_doa_plans(config)
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_dead_on_arrival_mints, args, config)
    assert "Dead-on-Arrival Plan Mints" in output
    assert "Window plans:     3" in output
    assert "Dead on arrival:  1" in output
    assert "DOA fraction:     33.3%" in output
    assert "  evolve: 1" in output


def test_cmd_measure_dead_on_arrival_mints_json(mem_fs: dict[str, str]) -> None:
    """--json round-trips through json.loads with the expected keys/values."""
    config = _make_config()
    config.ensure_dirs()
    _seed_doa_plans(config)
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_measure_dead_on_arrival_mints, args, config)
    data = json.loads(output)
    assert set(data) == {
        "total", "dead_on_arrival", "doa_fraction", "by_channel",
        "by_reason", "content_dead", "content_fraction",
    }
    assert data["total"] == 3
    assert data["dead_on_arrival"] == 1
    assert data["by_channel"] == {"evolve": 1}
    assert data["by_reason"] == {"saturation": 1}
    assert data["content_dead"] == 0


def test_cmd_measure_dead_on_arrival_mints_grounding_only_window(
    mem_fs: dict[str, str],
) -> None:
    """A window with only grounding deaths still prints its reason split."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.save_evolution_plan(_doa_cli_plan(
        "g1", obsolete_reason="[ungrounded:duplicate] grounding probe refuted"))
    memory.save_evolution_plan(_doa_cli_plan("ok", status="merged"))
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_dead_on_arrival_mints, args, config)
    assert "No dead-on-arrival" not in output
    assert "Dead on arrival:  0" in output
    assert "By mint channel:" not in output
    assert "By refusal reason (every door refusal):" in output
    assert "  grounding: 1" in output
    assert "Content deaths (grounding + restatement): 1 (50.0%)" in output


def _seed_value_and_flag_graduation_plans(config: Config) -> None:
    memory = Memory(config.memory_dir)
    memory.save_evolution_plan(_doa_cli_plan(
        "v1", obsolete_reason="[value-refused:no-claim] no admissible net-burn value"))
    memory.save_evolution_plan(_doa_cli_plan(
        "f1", obsolete_reason=(
            "[flag-graduation-premature:foo] graduates experimental flag foo "
            "before its review_by window closes")))
    memory.save_evolution_plan(_doa_cli_plan("ok", status="merged"))


def test_cmd_measure_dead_on_arrival_mints_value_and_flag_graduation_only_window(
    mem_fs: dict[str, str],
) -> None:
    """Value / flag-graduation-only refusals still print the reason table (#4592)."""
    config = _make_config()
    config.ensure_dirs()
    _seed_value_and_flag_graduation_plans(config)
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_dead_on_arrival_mints, args, config)
    assert "No dead-on-arrival" not in output
    assert "By refusal reason (every door refusal):" in output
    assert "  value: 1" in output
    assert "  flag-graduation: 1" in output
    assert "Dead on arrival:  0" in output
    assert "By mint channel:" not in output


def test_cmd_measure_dead_on_arrival_mints_value_and_flag_graduation_json(
    mem_fs: dict[str, str],
) -> None:
    """JSON mode keeps the counters saturation/content-only for these families."""
    config = _make_config()
    config.ensure_dirs()
    _seed_value_and_flag_graduation_plans(config)
    args = argparse.Namespace(last=None, json=True)
    data = json.loads(capture_stdout(cmd_measure_dead_on_arrival_mints, args, config))
    assert data["by_reason"] == {"value": 1, "flag-graduation": 1}
    assert data["dead_on_arrival"] == 0
    assert data["content_dead"] == 0


def test_cmd_measure_dead_on_arrival_mints_read_only(
    mem_fs: dict[str, str],
) -> None:
    """The command writes nothing: the in-memory FS is unchanged after the read."""
    config = _make_config()
    config.ensure_dirs()
    _seed_doa_plans(config)
    before = dict(mem_fs)
    args = argparse.Namespace(last=None, json=True)
    capture_stdout(cmd_measure_dead_on_arrival_mints, args, config)
    assert mem_fs == before


def test_cmd_measure_dead_on_arrival_mints_with_last(
    mem_fs: dict[str, str],
) -> None:
    """The --last flag bounds the window to the N most-recently-created plans.

    Mirrors :func:`test_cmd_measure_premise_accuracy_with_last`: an older
    door-obsoleted plan and a newer merged plan, windowed to last=1, keep only
    the newer non-door plan — so nothing reads as dead on arrival, proving the
    oldest was dropped rather than the door-obsoleted record kept.
    """
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.save_evolution_plan(
        _doa_cli_plan("old_doa", obsolete_reason=_DOA_CLI_REASON,
                      created_at="2026-01-01T00:00:00Z"))
    memory.save_evolution_plan(
        _doa_cli_plan("new_ok", status="merged",
                      created_at="2026-06-01T00:00:00Z"))
    args = argparse.Namespace(last=1, json=False)
    output = capture_stdout(cmd_measure_dead_on_arrival_mints, args, config)
    assert "No dead-on-arrival plan mints (window total: 1)." in output


def _seed_rejections(config: Config) -> None:
    """Seed 3 rejections oldest-first: already-shipped, not-needed, unrelated."""
    memory = Memory(config.memory_dir)
    memory.record_rejection(_rejection("r1", "This already ships in main"))
    memory.record_rejection(_rejection("r2", "meh", category="not_needed"))
    memory.record_rejection(_rejection("r3", "wrong approach"))


def test_measure_rejection_classes_subcommand_registered(
    mem_fs: dict[str, str],
) -> None:
    """main() routes 'measure-rejection-classes' to its handler."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-rejection-classes"]), \
         patch("maestro.cli.cmd_measure_rejection_classes") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_measure_rejection_classes_empty(mem_fs: dict[str, str]) -> None:
    """No rejections recorded prints the 'none' message."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_rejection_classes, args, config)
    assert "No rejections recorded." in output


def test_cmd_measure_rejection_classes_human_output(
    mem_fs: dict[str, str],
) -> None:
    """A seeded mix prints per-class counts and the any-class fraction."""
    config = _make_config()
    config.ensure_dirs()
    _seed_rejections(config)
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_rejection_classes, args, config)
    assert "Rejection Classes" in output
    assert "Window rejections: 3" in output
    assert "  already_shipped: 1" in output
    assert "  false_premise: 0" in output
    assert "  not_needed: 1" in output
    assert "Any grounding class: 2 (66.7%)" in output
    assert "Tree-checkable: " in output


def test_cmd_measure_rejection_classes_json(mem_fs: dict[str, str]) -> None:
    """--json round-trips with the expected keys/values."""
    config = _make_config()
    config.ensure_dirs()
    _seed_rejections(config)
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_measure_rejection_classes, args, config)
    data = json.loads(output)
    assert set(data) == {
        "total", "by_class", "any_grounding_class", "any_grounding_fraction",
        "tree_checkable",
    }
    assert data["total"] == 3
    assert data["any_grounding_class"] == 2


def test_cmd_measure_rejection_classes_with_last(
    mem_fs: dict[str, str],
) -> None:
    """--last 1 keeps only the newest (unrelated) rejection."""
    config = _make_config()
    config.ensure_dirs()
    _seed_rejections(config)
    args = argparse.Namespace(last=1, json=True)
    output = capture_stdout(cmd_measure_rejection_classes, args, config)
    data = json.loads(output)
    assert data["total"] == 1
    assert data["any_grounding_class"] == 0


def test_cmd_measure_rejection_classes_read_only(
    mem_fs: dict[str, str],
) -> None:
    """The command writes nothing: the in-memory FS is unchanged after the read."""
    config = _make_config()
    config.ensure_dirs()
    _seed_rejections(config)
    before = dict(mem_fs)
    args = argparse.Namespace(last=None, json=True)
    capture_stdout(cmd_measure_rejection_classes, args, config)
    assert mem_fs == before


# ---------------------------------------------------------------------------
# cmd_measure_gate_contention (proposal 126)
# ---------------------------------------------------------------------------


def _seed_gate_runs(config: Config) -> None:
    """Seed 4 gate-run rows across two in_flight buckets with mixed outcomes.

    Two single-tenant CLEAN runs (in_flight 0) and two contended runs (in_flight
    2): one TIMEOUT (a resource artifact) admitted on a slot and one FAILED that
    fell open under saturation (admission 'fallback'). So over the window the
    resource-artifact rate is 1/4 and the fallback rate is 1/4.
    """
    from maestro.gate_capacity import GateRunRecord, record_gate_run

    rows = [
        GateRunRecord("2026-09-01T00:00:00+00:00", 1.0, 4, 0, "slot", "CLEAN"),
        GateRunRecord("2026-09-01T00:01:00+00:00", 2.0, 4, 0, "slot", "CLEAN"),
        GateRunRecord("2026-09-01T00:02:00+00:00", 10.0, 2, 2, "slot", "TIMEOUT"),
        GateRunRecord("2026-09-01T00:03:00+00:00", 20.0, 1, 2, "fallback", "FAILED"),
    ]
    for row in rows:
        record_gate_run(config.gate_runs_path, row)


def test_measure_gate_contention_subcommand_registered(
    mem_fs: dict[str, str],
) -> None:
    """main() routes 'measure-gate-contention' to its handler.

    Reaching the handler proves argparse registered the subcommand — an
    unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-gate-contention"]), \
         patch("maestro.cli.cmd_measure_gate_contention") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_measure_gate_contention_empty_human(mem_fs: dict[str, str]) -> None:
    """An empty ledger prints the 'none' line rather than a table."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    assert "No gate runs recorded." in output


def test_cmd_measure_gate_contention_empty_json(mem_fs: dict[str, str]) -> None:
    """An empty ledger --json emits a well-formed zeroed report and exits 0."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    data = json.loads(output)
    assert data == {
        "total": 0,
        "buckets": [],
        "width_buckets": [],
        "resource_artifact_rate": 0.0,
        "fallback_rate": 0.0,
        "declined_rate": 0.0,
    }


def test_cmd_measure_gate_contention_human_output(mem_fs: dict[str, str]) -> None:
    """A seeded window prints per-bucket p95 and both rates."""
    config = _make_config()
    config.ensure_dirs()
    _seed_gate_runs(config)
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    assert "Gate-Run Contention" in output
    assert "Window runs: 4" in output
    assert "in_flight" in output
    assert "p95 (s)" in output
    # in_flight=2 bucket p95 nearest-rank of [10.0, 20.0] -> 20.00
    assert "20.00" in output
    assert "Resource-artifact rate: 25.0%" in output
    assert "Fallback-firing rate:   25.0%" in output
    # No row was refused by the runnability oracle, so the rate reads zero.
    assert "Declined rate:          0.0%" in output
    # The seeded rows measure no tests_seconds and record no budget, so every
    # width reports an unavailable fit, rendered as '-' rather than a number.
    assert "Fit by granted width (in-process runs):" in output
    assert "p50 tests (s)" in output
    assert "censored" in output
    assert "0.00       -" in output


def test_cmd_measure_gate_contention_prints_the_declined_rate(
    mem_fs: dict[str, str],
) -> None:
    """A refused run surfaces as the declined rate — the binding's evidence."""
    from maestro.gate_capacity import GateRunRecord, record_gate_run

    config = _make_config()
    config.ensure_dirs()
    record_gate_run(
        config.gate_runs_path,
        GateRunRecord("2026-09-21T00:00:00+00:00", 1.0, 4, 0, "slot", "CLEAN"),
    )
    record_gate_run(
        config.gate_runs_path,
        GateRunRecord(
            "2026-09-21T00:01:00+00:00", 0.01, 0, 0, "declined", "COLLECTION_ERROR",
        ),
    )
    args = argparse.Namespace(last=None, json=False)

    output = capture_stdout(cmd_measure_gate_contention, args, config)

    assert "Declined rate:          50.0%" in output


def _seed_measured_gate_runs(config: Config) -> None:
    """Seed two completed one-worker runs carrying a measured cost and a budget.

    Both finished CLEAN at width 1 having spent 400s and 600s of pytest time
    under an 800s budget, so the width's p50 cost is 400s against an 800s p50
    budget — a fit of 0.50 and no censored runs.
    """
    from maestro.gate_capacity import GateRunRecord, record_gate_run

    for started, tests_seconds in (
        ("2026-09-02T00:00:00+00:00", 400.0),
        ("2026-09-02T00:01:00+00:00", 600.0),
    ):
        record_gate_run(
            config.gate_runs_path,
            GateRunRecord(
                started, 900.0, 1, 0, "slot", "CLEAN",
                deadline_seconds=800, tests_seconds=tests_seconds,
            ),
        )


def test_cmd_measure_gate_contention_renders_a_measured_fit(
    mem_fs: dict[str, str],
) -> None:
    """A width with both a measured cost and a granted budget prints its ratio."""
    config = _make_config()
    config.ensure_dirs()
    _seed_measured_gate_runs(config)
    args = argparse.Namespace(last=None, json=False)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    assert "Fit by granted width (in-process runs):" in output
    assert "400.00" in output
    assert "800.00" in output
    assert "0.50" in output


def test_cmd_measure_gate_contention_json_carries_the_fit_bucket(
    mem_fs: dict[str, str],
) -> None:
    """--json round-trips the fit bucket with its documented keys."""
    config = _make_config()
    config.ensure_dirs()
    _seed_measured_gate_runs(config)
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    data = json.loads(output)
    assert data["width_buckets"] == [
        {
            "workers": 1,
            "count": 2,
            "p50_tests_seconds": 400.0,
            "p50_deadline_seconds": 800.0,
            "fit": 0.5,
            "censored": 0,
        },
    ]


def test_cmd_measure_gate_contention_json(mem_fs: dict[str, str]) -> None:
    """--json round-trips through json.loads with the documented keys/values."""
    config = _make_config()
    config.ensure_dirs()
    _seed_gate_runs(config)
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    data = json.loads(output)
    assert set(data) == {
        "total", "buckets", "width_buckets",
        "resource_artifact_rate", "fallback_rate", "declined_rate",
    }
    assert data["total"] == 4
    assert data["resource_artifact_rate"] == 0.25
    assert data["fallback_rate"] == 0.25
    assert data["declined_rate"] == 0.0
    assert [b["in_flight"] for b in data["buckets"]] == [0, 2]
    assert [b["workers"] for b in data["width_buckets"]] == [1, 2, 4]


def test_cmd_measure_gate_contention_read_only(mem_fs: dict[str, str]) -> None:
    """The command writes nothing: the in-memory FS is unchanged after the read."""
    config = _make_config()
    config.ensure_dirs()
    _seed_gate_runs(config)
    before = dict(mem_fs)
    args = argparse.Namespace(last=None, json=True)
    capture_stdout(cmd_measure_gate_contention, args, config)
    assert mem_fs == before


def test_cmd_measure_gate_contention_with_last(mem_fs: dict[str, str]) -> None:
    """--last bounds the window to the N most recent rows.

    Seeding four rows and windowing to last=1 keeps only the newest run, so the
    reported total is 1 — proving the older rows were dropped from the window.
    """
    config = _make_config()
    config.ensure_dirs()
    _seed_gate_runs(config)
    args = argparse.Namespace(last=1, json=True)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    data = json.loads(output)
    assert data["total"] == 1


def test_cmd_measure_gate_contention_skips_corrupted_row(
    mem_fs: dict[str, str],
) -> None:
    """A deliberately corrupted ledger line is skipped rather than fatal."""
    config = _make_config()
    config.ensure_dirs()
    _seed_gate_runs(config)
    # Append a corrupt line to the ledger.
    existing = mem_fs[str(config.gate_runs_path)]
    config.gate_runs_path.write_text(existing + "this is not json\n")
    args = argparse.Namespace(last=None, json=True)
    output = capture_stdout(cmd_measure_gate_contention, args, config)
    data = json.loads(output)
    assert data["total"] == 4


def test_cmd_measure_gate_contention_skips_wrong_typed_row(
    mem_fs: dict[str, str],
) -> None:
    """A complete-but-wrong-typed ledger row is skipped, not fatal.

    The row is valid JSON with every required key present, so only value
    validation at the parse boundary keeps it out of the concurrency buckets,
    whose sort and hash a None in_flight would otherwise crash with a TypeError.
    Both the human and --json paths must report the valid neighbours instead.
    """
    config = _make_config()
    config.ensure_dirs()
    _seed_gate_runs(config)
    bad = {
        "started_at": "2026-09-01T00:04:00+00:00",
        "duration_seconds": None,
        "workers": 4,
        "in_flight": None,
        "admission": "slot",
        "outcome": "CLEAN",
    }
    existing = mem_fs[str(config.gate_runs_path)]
    config.gate_runs_path.write_text(existing + json.dumps(bad) + "\n")

    output = capture_stdout(
        cmd_measure_gate_contention,
        argparse.Namespace(last=None, json=True),
        config,
    )
    data = json.loads(output)
    assert data["total"] == 4
    assert [b["in_flight"] for b in data["buckets"]] == [0, 2]

    human = capture_stdout(
        cmd_measure_gate_contention,
        argparse.Namespace(last=None, json=False),
        config,
    )
    assert "Window runs: 4" in human


def test_cmd_measure_gate_contention_skips_unhashable_in_flight(
    mem_fs: dict[str, str],
) -> None:
    """A list concurrency is skipped rather than raising 'unhashable type'.

    Distinct from the None case: this one would crash at the dict key, before
    any sort, so it exercises the other half of the bucketing invariant.
    """
    config = _make_config()
    config.ensure_dirs()
    _seed_gate_runs(config)
    bad = {
        "started_at": "2026-09-01T00:04:00+00:00",
        "duration_seconds": 1.0,
        "workers": 4,
        "in_flight": [],
        "admission": "slot",
        "outcome": "CLEAN",
    }
    existing = mem_fs[str(config.gate_runs_path)]
    config.gate_runs_path.write_text(existing + json.dumps(bad) + "\n")
    args = argparse.Namespace(last=None, json=True)
    data = json.loads(capture_stdout(cmd_measure_gate_contention, args, config))
    assert data["total"] == 4
    assert [b["in_flight"] for b in data["buckets"]] == [0, 2]


# ---------------------------------------------------------------------------
# cmd_list_gates
# ---------------------------------------------------------------------------


def test_cmd_list_gates_prints_chain_in_precedence_order() -> None:
    """list-gates prints every registry entry, blockers before bridges."""
    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_list_gates, args, config)

    assert "Task-generation gate chain (precedence order)" in output
    pos_quiet = output.index("quiet_skip")
    pos_audit = output.index("audit_stall")
    pos_cost = output.index("cost_risk")
    pos_instrument = output.index("instrument_consumption")
    pos_forced = output.index("forced_read")
    assert pos_quiet < pos_audit
    assert pos_audit < pos_cost
    assert pos_cost < pos_instrument
    assert pos_instrument < pos_forced


def test_cmd_list_gates_labels_kinds_and_postcall_note() -> None:
    """Each entry carries its kind; the post-call content gates are footnoted."""
    from maestro.task_gates import content_gate_order

    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_list_gates, args, config)

    lines = output.splitlines()
    block_lines = [ln for ln in lines if ln.rstrip().endswith(" block")]
    directive_lines = [ln for ln in lines if ln.rstrip().endswith(" directive")]
    assert len(block_lines) == 7
    assert len(directive_lines) == 15
    assert "Post-call content gates" in output
    # Read the membership off the single declaration rather than re-hardcoding
    # four names, so this assertion cannot become a seventh drifting copy.
    assert ", ".join(content_gate_order()) in output
    assert "_vet_goal_content" in output


def test_main_dispatches_list_gates(mem_fs: dict[str, str]) -> None:
    """main() dispatches to cmd_list_gates for 'list-gates'."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "list-gates"]), \
         patch("maestro.cli.cmd_list_gates") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


# ── cmd_measure_false_success ──────────────────────────────────────


def _make_remediation_task(
    task_id: str,
    pr_number: int,
    outcome: str = "success",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_number}: fix failing CI gate",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="No remediation needed",
        errors=[],
        duration_secs=60.0,
        timestamp="2026-05-26T10:00:00",
        source="pending_pr_requests",  # type: ignore[arg-type]
    )


def test_cmd_measure_false_success_twice_stamped_pr(
    mem_fs: dict[str, str],
) -> None:
    """A PR stamped success twice is reported with count 1."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_remediation_task("t1", 909))
    memory.record_task(_make_remediation_task("t2", 909))

    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_false_success, args, config)

    assert "False-Success Recurrence Report" in output
    assert "PRs stamped success >=2x without closing: 1" in output
    assert "#909: 2 success outcomes" in output


def test_cmd_measure_false_success_empty(mem_fs: dict[str, str]) -> None:
    """Empty history prints a zero report with no By PR section."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_false_success, args, config)
    assert "PRs stamped success >=2x without closing: 0" in output
    assert "By PR:" not in output


# ── cmd_measure_outcome_trends ─────────────────────────────────────


def _make_trend_task(
    task_id: str,
    outcome: TaskOutcome,
    goal: str = "Extend the executor module with retry logic",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[],
        outcome=outcome,
        result_summary="done",
        errors=[],
        duration_secs=60.0,
        timestamp="2026-05-26T10:00:00",
        source="self_directed",
    )


def test_cmd_measure_outcome_trends_reports_trend(
    mem_fs: dict[str, str],
) -> None:
    """A window of mixed outcomes prints a directional trend summary."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    # Oldest 4 failed, newest 4 success -> improving success trend.
    for i in range(4):
        memory.record_task(_make_trend_task(f"old{i}", "failure"))
    for i in range(4):
        memory.record_task(_make_trend_task(f"new{i}", "success"))

    args = argparse.Namespace(window=8)
    output = capture_stdout(cmd_measure_outcome_trends, args, config)

    assert "Outcome Trend Report" in output
    assert "OUTCOME TREND" in output
    assert "↑" in output


def test_cmd_measure_outcome_trends_insufficient_data(
    mem_fs: dict[str, str],
) -> None:
    """Fewer than four tasks prints the insufficient-data message."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_trend_task("t1", "success"))

    args = argparse.Namespace(window=20)
    output = capture_stdout(cmd_measure_outcome_trends, args, config)

    assert "Outcome Trend Report" in output
    assert "Insufficient data" in output


def test_cmd_measure_outcome_trends_window_default(
    mem_fs: dict[str, str],
) -> None:
    """Non-positive window falls back to the default of 20."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    for i in range(6):
        memory.record_task(_make_trend_task(f"t{i}", "success"))

    args = argparse.Namespace(window=0)
    output = capture_stdout(cmd_measure_outcome_trends, args, config)

    assert "OUTCOME TREND" in output


# ── cmd_measure_handoff_relapse ────────────────────────────────────


def test_cmd_measure_handoff_relapse_reports_relapse(
    mem_fs: dict[str, str],
) -> None:
    """A task re-engaging a handed-off lineage prints HANDOFF-RELAPSE."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(
        _make_trend_task(
            "relapse1", "success", goal="Remediate PR #2579: redo",
        )
    )
    # The relapse signature reads result_summary; seed it with the lineage ref.
    memory.record_task(
        TaskRecord(
            task_id="relapse2",
            goal="Re-engage PR #2579",
            plan=[],
            outcome="success",
            result_summary="Re-read PR #2579 merge blocker again",
            errors=[],
            duration_secs=10.0,
            timestamp="2026-06-13T10:00:00",
            source="self_directed",
        )
    )
    store = MessageStore(config.memory_dir)
    store.save_message(
        Message(
            message_id="handoff-2579-abc12345",
            timestamp="2026-06-13T00:00:00+00:00",
            sender="maestro",
            content="Handoff — PR #2579 carries an implemented cure ...",
            message_type="question",
        )
    )

    args = argparse.Namespace(window=50)
    output = capture_stdout(cmd_measure_handoff_relapse, args, config)

    assert "Handoff-Relapse Report" in output
    assert "HANDOFF-RELAPSE" in output
    assert "#2579" in output


def test_cmd_measure_handoff_relapse_no_relapse(
    mem_fs: dict[str, str],
) -> None:
    """Without a prior handoff, the no-relapse message prints."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(
        TaskRecord(
            task_id="t1",
            goal="Re-engage PR #2579",
            plan=[],
            outcome="success",
            result_summary="Re-read PR #2579",
            errors=[],
            duration_secs=10.0,
            timestamp="2026-06-13T10:00:00",
            source="self_directed",
        )
    )

    args = argparse.Namespace(window=0)
    output = capture_stdout(cmd_measure_handoff_relapse, args, config)

    assert "Handoff-Relapse Report" in output
    assert "No recent task re-engaged" in output


# ── cmd_measure_relap ──────────────────────────────────────────────


def test_cmd_measure_relap_reports_rate(mem_fs: dict[str, str]) -> None:
    """Settled-relap no-ops are separated from live-fork no-ops in the report."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1",
        result_summary="No remediation needed — PR already merged into main.",
    ))
    memory.record_task(_make_noop_task(
        task_id="t2", result_summary="Already resolved upstream.",
    ))
    memory.record_task(_make_noop_task(
        task_id="t3",
        result_summary="Confirmed existing state, awaiting tend push.",
    ))
    memory.record_task(_make_noop_task(
        task_id="t4", result_summary="Shipped a new feature.",
    ))

    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_relap, args, config)

    assert "Settled-Relap Report" in output
    assert "No-op tasks:        3" in output
    assert "Settled-relaps:     2" in output
    assert "Live-fork no-ops:   1" in output
    assert "Relap rate:         66.7%" in output


def test_cmd_measure_relap_empty(mem_fs: dict[str, str]) -> None:
    """Empty history prints a zero report."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_relap, args, config)

    assert "Settled-Relap Report" in output
    assert "No-op tasks:        0" in output
    assert "Relap rate:         0.0%" in output


# ── cmd_measure_confirmed_landings ─────────────────────────────────


def test_cmd_measure_confirmed_landings_reports_rate(
    mem_fs: dict[str, str],
) -> None:
    """CAUSED and CONFIRMED landings are tallied and CONFIRMED examples listed."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_task(_make_noop_task(
        task_id="t1",
        result_summary="Merged origin/main and resolved the conflict.",
    ))
    memory.record_task(_make_noop_task(
        task_id="t2",
        result_summary="PR was already merged before this task.",
    ))
    memory.record_task(_make_noop_task(
        task_id="t3", result_summary="git shows merged; confirmed merged.",
    ))

    args = argparse.Namespace(last=10)
    output = capture_stdout(cmd_measure_confirmed_landings, args, config)

    assert "Confirmed-vs-Caused Landing Report" in output
    assert "Caused landings:     1" in output
    assert "Confirmed landings:  2" in output
    assert "Attributable total:  3" in output
    assert "Confirmed rate:      66.7%" in output
    assert "Recent CONFIRMED examples" in output
    assert "t2" in output


def test_cmd_measure_confirmed_landings_empty(mem_fs: dict[str, str]) -> None:
    """Empty history prints a zero report with no examples block."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_confirmed_landings, args, config)

    assert "Confirmed-vs-Caused Landing Report" in output
    assert "Attributable total:  0" in output
    assert "Confirmed rate:      0.0%" in output
    assert "Recent CONFIRMED examples" not in output


# ── cmd_pr_laps ────────────────────────────────────────────────────


def test_cmd_pr_laps_surfaces_pr_at_half_budget(mem_fs: dict[str, str]) -> None:
    """A PR at >= budget/2 unlanded laps appears in the report and PR-LAPS line."""
    config = _make_config(pr_remediation_lap_budget=6)
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    # Three unlanded partial laps on #770 → 3 == budget/2, so it surfaces.
    for i in range(3):
        memory.record_task(
            _make_remediation_task(f"t{i}", 770, outcome="partial"),
        )

    args = argparse.Namespace(window=None)
    with patch(
        "maestro.audit_checks_phantom._git_pr_merged_default", return_value=False,
    ):
        output = capture_stdout(cmd_pr_laps, args, config)

    assert "PR Remediation Lap Report" in output
    assert "Lap budget (per PR): 6" in output
    assert "#770: 3/6 laps" in output
    assert "PR-LAPS:" in output


def test_cmd_pr_laps_annotates_standdown(mem_fs: dict[str, str]) -> None:
    """A PR with an active standdown is listed and annotated even at zero laps."""
    from maestro.escalation_bridge import _standdowns_path, _write_standdowns

    config = _make_config(pr_remediation_lap_budget=6)
    config.ensure_dirs()
    _write_standdowns(
        _standdowns_path(config.memory_dir),
        {880: {"head_sha": "abc1234", "delivered_at": "2026-06-16T00:00:00Z"}},
    )

    args = argparse.Namespace(window=None)
    output = capture_stdout(cmd_pr_laps, args, config)

    assert "#880: 0/6 laps [stood-down]" in output


def test_cmd_pr_laps_empty(mem_fs: dict[str, str]) -> None:
    """No unlanded laps and no standdowns prints the empty notice, no PR-LAPS."""
    config = _make_config(pr_remediation_lap_budget=6)
    config.ensure_dirs()

    args = argparse.Namespace(window=None)
    output = capture_stdout(cmd_pr_laps, args, config)

    assert "PR Remediation Lap Report" in output
    assert "No unlanded remediation laps or standdowns recorded." in output
    assert "PR-LAPS:" not in output


def test_cmd_pr_laps_custom_window_excludes_older_laps(
    mem_fs: dict[str, str],
) -> None:
    """A positive --window flows into the scan, excluding out-of-window laps.

    Exercises the ``args.window if args.window and args.window > 0`` true-branch
    that the default-window tests (``window=None``) never reach, so a regression
    that ignored the flag (scanning the constant instead of ``window``) would be
    caught here.
    """
    config = _make_config(pr_remediation_lap_budget=6)
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    # Oldest record: one unlanded lap on #770. Then three newer laps on #880.
    memory.record_task(_make_remediation_task("old", 770, outcome="partial"))
    for i in range(3):
        memory.record_task(
            _make_remediation_task(f"new{i}", 880, outcome="partial"),
        )

    # Window of 3 scans only the three most-recent records (all #880), so the
    # older #770 lap falls outside the window and is not surfaced. With the
    # default window it would appear, so its absence proves the flag is honored.
    args = argparse.Namespace(window=3)
    with patch(
        "maestro.audit_checks_phantom._git_pr_merged_default", return_value=False,
    ):
        output = capture_stdout(cmd_pr_laps, args, config)

    assert "Window scanned:" in output
    assert "3 tasks" in output
    assert "#880: 3/6 laps" in output
    assert "#770" not in output


def test_make_trend_task_uses_valid_schema_values(
    mem_fs: dict[str, str],
) -> None:
    """_make_trend_task must produce TaskRecords with valid TaskOutcome and TaskSource values."""
    from typing import get_args

    valid_outcomes = get_args(TaskOutcome)
    valid_sources = get_args(TaskSource)

    task = _make_trend_task("schema-check", "failure")
    assert task.outcome in valid_outcomes
    assert task.source in valid_sources

    task_success = _make_trend_task("schema-check-2", "success")
    assert task_success.outcome in valid_outcomes
    assert task_success.source in valid_sources


def test_cmd_measure_false_success_last_none_reads_history(
    mem_fs: dict[str, str],
) -> None:
    """When last=None, all tasks are examined (not zero).

    This guards against the regression where ``else 0`` caused
    ``get_recent_tasks(n=0)`` to return ``[]`` when ``--last`` is omitted.
    """
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    # Seed two success-stamped remediation tasks for the same PR
    memory.record_task(_make_remediation_task("t1", 909))
    memory.record_task(_make_remediation_task("t2", 909))

    args = argparse.Namespace(last=None)
    output = capture_stdout(cmd_measure_false_success, args, config)

    # With last=None all tasks must be read — both remediation tasks examined.
    assert "PRs stamped success >=2x without closing: 1" in output
    assert "#909: 2 success outcomes" in output
