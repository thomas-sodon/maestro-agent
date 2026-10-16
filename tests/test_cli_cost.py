"""cost-report (with --by-caller), cost-per-task, list-versions and burn-baseline; split
out of ``tests/test_cli.py``.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from maestro.cli import (
    _build_version_archive_backend,
    cmd_burn_baseline,
    cmd_cost_per_task,
    cmd_cost_report,
    cmd_list_versions,
)
from maestro.config import Config
from maestro.memory import (
    Memory,
)
from tests.conftest import capture_stdout
from tests.test_cli import (
    _make_cli_version,
    _make_config,
)


def test_cost_report_subcommand_registered(mem_fs: dict[str, str]) -> None:
    """main() routes the 'cost-report' subcommand to cmd_cost_report.

    Reaching the handler at all proves argparse registered the subcommand —
    an unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "cost-report"]), \
         patch("maestro.cli.cmd_cost_report") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_cost_report_no_data(mem_fs: dict[str, str]) -> None:
    """With no usage.jsonl, the command prints the no-data message."""
    config = _make_config()
    args = argparse.Namespace(days=7, json=False, by_caller=False)
    output = capture_stdout(cmd_cost_report, args, config)
    assert "No usage data" in output


def test_cmd_cost_report_with_data(mem_fs: dict[str, str]) -> None:
    """With usage rows present, the command prints a total-cost summary."""
    config = _make_config()
    config.ensure_dirs()
    rows = [
        {
            "iteration": 1,
            "started_at": "2026-06-14T10:00:00+00:00",
            "calls": 1,
            "total_cost_usd": 0.42,
            "providers": ["anthropic"],
        },
    ]
    mem_fs[str(config.usage_log_path)] = (
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )

    args = argparse.Namespace(days=30, json=False, by_caller=False)
    output = capture_stdout(cmd_cost_report, args, config)
    assert "Total cost" in output


def test_cmd_cost_report_defaults_nonpositive_days(mem_fs: dict[str, str]) -> None:
    """A non-positive --days falls back to the 7-day default."""
    config = _make_config()
    config.ensure_dirs()
    mem_fs[str(config.usage_log_path)] = json.dumps({
        "iteration": 1,
        "started_at": "2026-06-14T10:00:00+00:00",
        "total_cost_usd": 0.10,
        "providers": ["anthropic"],
    }) + "\n"

    args = argparse.Namespace(days=0, json=False, by_caller=False)
    output = capture_stdout(cmd_cost_report, args, config)
    assert "last 7 days" in output


def _seed_caller_spend(mem_fs: dict[str, str], config: Config) -> None:
    """Seed in-window samples (one unmetered) and two metered in-window iterations.

    An out-of-window sample and iteration, plus an unmetered in-window
    iteration, are seeded too so spend and iterations are pinned to one
    metered window.
    """
    from dataclasses import asdict

    from maestro.cost_tracking import CostSample, _clear_cost_rate_cache

    _clear_cost_rate_cache()
    now = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    samples = [
        CostSample(old, 100.0, 1, 1, 0, 0, "reflect", 10),
        CostSample(now, 3.0, 1, 1, 0, 0, "reflect", 10),
        CostSample(now, 1.0, 1, 1, 0, 0, "propose_plan", 10),
        CostSample(now, 50.0, 0, 0, 0, 0, "ghost", 10, unmetered=True),
    ]
    mem_fs[str(config.cost_samples_path)] = (
        "\n".join(json.dumps(asdict(s)) for s in samples) + "\n"
    )
    rows = [
        {"iteration": 0, "started_at": old, "total_cost_usd": 0.0},
        {"iteration": 1, "started_at": now, "total_cost_usd": 0.0},
        {"iteration": 2, "started_at": now, "total_cost_usd": 0.0},
        {"iteration": 3, "started_at": now, "total_cost_usd": 0.0, "unmetered": True},
    ]
    mem_fs[str(config.usage_log_path)] = "".join(json.dumps(r) + "\n" for r in rows)


def test_cost_report_by_caller_flag_parses(mem_fs: dict[str, str]) -> None:
    """``cost-report --by-caller`` reaches the handler with the flag set."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "cost-report", "--by-caller", "--json"]), \
         patch("maestro.cli.cmd_cost_report") as mock_cmd:
        main()

    args = mock_cmd.call_args.args[0]
    assert args.by_caller is True
    assert args.json is True


def test_cmd_cost_report_by_caller_json_reports_exact_figures(
    mem_fs: dict[str, str],
) -> None:
    """--by-caller --json adds a ``by_caller`` reading with exact figures."""
    config = _make_config()
    config.ensure_dirs()
    _seed_caller_spend(mem_fs, config)

    args = argparse.Namespace(days=30, json=True, by_caller=True)
    with patch("maestro.cli.SubprocessCodeHost") as host, \
         patch("maestro.cli.SubprocessVCS") as vcs:
        output = capture_stdout(cmd_cost_report, args, config)
    host.assert_not_called()
    vcs.assert_not_called()

    reading = json.loads(output)["caller_spend"]
    assert reading["window_days"] == 30
    assert reading["iterations"] == 2
    assert reading["spend_usd"] == pytest.approx(4.0)
    assert reading["spend_usd_per_iteration"] == pytest.approx(2.0)
    assert "income_source" not in reading
    callers = [row["caller"] for row in reading["by_caller"]]
    assert callers == ["reflect", "propose_plan"]


def test_cmd_cost_report_json_omits_by_caller_without_flag(
    mem_fs: dict[str, str],
) -> None:
    config = _make_config()
    config.ensure_dirs()
    _seed_caller_spend(mem_fs, config)
    args = argparse.Namespace(days=30, json=True, by_caller=False)
    assert "by_caller" not in json.loads(capture_stdout(cmd_cost_report, args, config))


def test_cmd_cost_report_by_caller_text_lists_top_callers(
    mem_fs: dict[str, str],
) -> None:
    """The text path appends the per-caller block after the report."""
    config = _make_config()
    config.ensure_dirs()
    _seed_caller_spend(mem_fs, config)

    output = capture_stdout(
        cmd_cost_report, argparse.Namespace(days=30, json=False, by_caller=True), config,
    )

    assert "Spend by caller — last 30 days, 2 iterations" in output
    assert "Spend/iteration:  $2.0000" in output
    assert "reflect (" in output
    assert "$1.5000/iteration" in output
    assert "Income" not in output


def test_cmd_cost_report_by_caller_no_data(mem_fs: dict[str, str]) -> None:
    """With no data the per-iteration figure is unknown and no caller is listed."""
    from maestro.cost_tracking import _clear_cost_rate_cache

    _clear_cost_rate_cache()
    config = _make_config()

    output = capture_stdout(
        cmd_cost_report, argparse.Namespace(days=7, json=False, by_caller=True), config,
    )

    assert "0 iterations" in output
    assert "Spend/iteration:  n/a" in output
    assert "No metered calls in the window." in output
    assert "Top callers" not in output


def test_cmd_cost_report_by_caller_clamps_window_to_archive_threshold(
    mem_fs: dict[str, str],
) -> None:
    """With archival on, the window clamps to the hot span and says so."""
    from maestro.cost_tracking import _clear_cost_rate_cache

    _clear_cost_rate_cache()
    config = _make_config(archive_threshold_days=8)

    payload = json.loads(capture_stdout(
        cmd_cost_report, argparse.Namespace(days=30, json=True, by_caller=True), config,
    ))
    assert payload["caller_spend"]["window_days"] == 8
    assert payload["caller_spend"]["requested_window_days"] == 30

    _clear_cost_rate_cache()
    text = capture_stdout(
        cmd_cost_report, argparse.Namespace(days=30, json=False, by_caller=True), config,
    )
    assert "last 8 days" in text
    assert "clamped from 30 days" in text


def test_cost_per_task_subcommand_registered(mem_fs: dict[str, str]) -> None:
    """main() routes the 'cost-per-task' subcommand to cmd_cost_per_task."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "cost-per-task", "task-1"]), \
         patch("maestro.cli.cmd_cost_per_task") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_cost_per_task_known_id(mem_fs: dict[str, str]) -> None:
    """A known task_id prints its summed cost and exits 0."""
    config = _make_config()
    config.ensure_dirs()
    rows = [
        {"task_id": "task-7", "total_cost_usd": 0.25},
        {"task_id": "task-7", "total_cost_usd": 0.17},
        {"task_id": "other", "total_cost_usd": 9.99},
    ]
    mem_fs[str(config.usage_log_path)] = (
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )

    args = argparse.Namespace(task_id="task-7")
    # The known-id branch must exit 0: it returns normally with no
    # SystemExit. Capture stdout directly via redirect_stdout (which does
    # NOT swallow SystemExit, unlike capture_stdout) so a regression that
    # calls sys.exit(<non-zero>) on the success path propagates and fails
    # the test rather than passing silently.
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_cost_per_task(args, config)
    assert "Task task-7: $0.4200" in buf.getvalue()


def test_cmd_cost_per_task_unknown_id(mem_fs: dict[str, str]) -> None:
    """An unknown task_id prints the no-data message and exits non-zero."""
    config = _make_config()
    config.ensure_dirs()
    mem_fs[str(config.usage_log_path)] = json.dumps(
        {"task_id": "task-7", "total_cost_usd": 0.25}
    ) + "\n"

    args = argparse.Namespace(task_id="missing")
    with pytest.raises(SystemExit) as excinfo:
        cmd_cost_per_task(args, config)
    assert excinfo.value.code == 1


def test_cmd_cost_per_task_unknown_id_message(mem_fs: dict[str, str]) -> None:
    """The unknown-id branch prints a distinct no-data message."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(task_id="missing")
    output = capture_stdout(cmd_cost_per_task, args, config)
    assert "No usage data for task missing" in output


def test_cmd_cost_report_json_emits_aggregates(mem_fs: dict[str, str]) -> None:
    """--json prints one parseable JSON object with per-date/provider totals.

    Seeds an in-memory usage.jsonl spanning two UTC dates and two providers,
    then asserts the JSON branch parses, carries the expected keys, sums the
    grand total and per-dimension aggregates correctly, and exits 0.
    """
    config = _make_config()
    config.ensure_dirs()
    rows = [
        {
            "iteration": 1,
            "started_at": "2026-07-10T10:00:00+00:00",
            "total_cost_usd": 0.40,
            "providers": ["anthropic"],
        },
        {
            "iteration": 2,
            "started_at": "2026-07-10T12:00:00+00:00",
            "total_cost_usd": 0.10,
            "providers": ["anthropic"],
        },
        {
            "iteration": 3,
            "started_at": "2026-07-11T09:00:00+00:00",
            "total_cost_usd": 0.20,
            "providers": ["openai"],
        },
    ]
    mem_fs[str(config.usage_log_path)] = (
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )

    # A fixed reference date keeps every seeded row inside the 30-day window.
    now = datetime(2026, 7, 11, 15, 0, tzinfo=UTC)
    args = argparse.Namespace(days=30, json=True, by_caller=False)
    with patch("maestro.cost_tracking.datetime") as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat
        output = capture_stdout(cmd_cost_report, args, config)

    payload = json.loads(output)  # single parseable object or this raises
    assert payload["status"] == "ok"
    assert payload["days"] == 30
    assert payload["iterations"] == 3
    assert payload["total_cost_usd"] == pytest.approx(0.70)
    assert payload["average_cost_usd"] == pytest.approx(0.70 / 3)
    assert payload["by_date"] == {
        "2026-07-10": pytest.approx(0.50),
        "2026-07-11": pytest.approx(0.20),
    }
    assert payload["by_provider"] == {
        "anthropic": pytest.approx(0.50),
        "openai": pytest.approx(0.20),
    }
    # The per-dimension aggregates each sum to the grand total.
    assert sum(payload["by_date"].values()) == pytest.approx(0.70)
    assert sum(payload["by_provider"].values()) == pytest.approx(0.70)


def test_cmd_cost_report_json_no_data(mem_fs: dict[str, str]) -> None:
    """--json with no log still prints a parseable object and exits 0."""
    config = _make_config()
    args = argparse.Namespace(days=7, json=True, by_caller=False)
    output = capture_stdout(cmd_cost_report, args, config)

    payload = json.loads(output)
    assert payload["status"] == "no_data"
    assert payload["iterations"] == 0
    assert payload["total_cost_usd"] == 0.0
    assert payload["average_cost_usd"] == 0.0
    assert payload["by_date"] == {}
    assert payload["by_provider"] == {}


def test_cmd_cost_report_default_text_branch_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """Without --json the command prints the text table, not JSON.

    Regression guard for acceptance criterion #2: the same seeded data that
    the JSON branch reports must still render the human table verbatim, and
    that output must not be JSON-parseable.
    """
    config = _make_config()
    config.ensure_dirs()
    mem_fs[str(config.usage_log_path)] = json.dumps({
        "iteration": 1,
        "started_at": "2026-07-10T10:00:00+00:00",
        "total_cost_usd": 0.42,
        "providers": ["anthropic"],
    }) + "\n"

    args = argparse.Namespace(days=30, json=False, by_caller=False)
    output = capture_stdout(cmd_cost_report, args, config)

    assert output.startswith("Cost Report (last 30 days)")
    assert "By provider:" in output
    assert "Total cost: $0.4200" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_cmd_list_versions_with_data(mem_fs: dict[str, str]) -> None:
    """A populated archive prints sha, title, date, and gate flag per row."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_version(_make_cli_version())
    memory.record_version(_make_cli_version(
        version_id="v2",
        plan_title="Second merged plan",
        commit_sha="0011223344556677",
        merged_at="2026-06-25T09:30:00+00:00",
        quality_gate_pass=False,
    ))

    args = argparse.Namespace(limit=30)
    output = capture_stdout(cmd_list_versions, args, config)

    assert "Merged Versions" in output
    # sha truncated to 12 chars, plan title, merge date, and gate flag.
    assert "abcdef012345" in output
    assert "Add list-versions command" in output
    assert "2026-06-24T12:00:00+00:00" in output
    assert "gate=pass" in output
    # The CI-failed merge surfaces as gate=FAIL.
    assert "Second merged plan" in output
    assert "gate=FAIL" in output


def test_cmd_list_versions_empty_archive(mem_fs: dict[str, str]) -> None:
    """An empty/absent archive prints the explicit no-versions message."""
    config = _make_config()
    args = argparse.Namespace(limit=30)
    output = capture_stdout(cmd_list_versions, args, config)
    assert output.strip() == "no merged versions yet"


@patch("maestro.cli.StateStore")
def test_cmd_list_versions_flag_on_reads_sqlite_not_frozen_jsonl(
    mock_store_cls: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on, list-versions reads ``state.db``, not the frozen JSONL.

    Regression for PR #3473: ``cmd_list_versions`` built an un-injected
    ``Memory``, so under ``sqlite_state_enabled`` it read the frozen rollback
    ``version_archive.jsonl`` and missed every version recorded to ``state.db``
    after the flag flip. ``StateStore`` is patched to a shared real in-memory
    store so both the seeding write and the CLI read share one connection with
    no real ``state.db`` file. A version written only through the SQLite-backed
    (loop-owning) ``Memory`` — with the legacy JSONL absent — must surface.
    """
    from maestro.state_store import StateStore as RealStateStore

    config = _make_config(sqlite_state_enabled=True)
    config.ensure_dirs()
    mock_store_cls.return_value = RealStateStore(":memory:")
    loop_memory = Memory(
        config.memory_dir,
        version_archive=_build_version_archive_backend(config),
    )
    loop_memory.record_version(_make_cli_version(
        plan_title="SQLite-only merged plan",
        commit_sha="feedfacecafebabe",
    ))

    args = argparse.Namespace(limit=30)
    output = capture_stdout(cmd_list_versions, args, config)
    # Surfaced from state.db even though version_archive.jsonl was never written.
    assert "SQLite-only merged plan" in output
    assert "feedfacecafe" in output
    assert str(config.memory_dir / "version_archive.jsonl") not in mem_fs


def test_cmd_list_versions_limit_default(mem_fs: dict[str, str]) -> None:
    """A non-positive --limit falls back to the default of 30."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.record_version(_make_cli_version())

    args = argparse.Namespace(limit=0)
    output = capture_stdout(cmd_list_versions, args, config)
    assert "Add list-versions command" in output


def test_list_versions_subcommand_registered(mem_fs: dict[str, str]) -> None:
    """main() routes the 'list-versions' subcommand to cmd_list_versions."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "list-versions"]), \
         patch("maestro.cli.cmd_list_versions") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_burn_baseline_subcommand_registered(mem_fs: dict[str, str]) -> None:
    """main() routes the 'burn-baseline' subcommand to cmd_burn_baseline.

    Reaching the handler at all proves argparse registered the subcommand —
    an unregistered name exits with argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "burn-baseline"]), \
         patch("maestro.cli.cmd_burn_baseline") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_burn_baseline_no_data(mem_fs: dict[str, str]) -> None:
    """With no usage/task corpora, the baseline prints the placeholder table."""
    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_burn_baseline, args, config)
    assert "Burn-rate baseline" in output
    assert "no task-attributable spend" in output
    assert "Prompt-cache hit rate: 0.0%" in output


def test_cmd_burn_baseline_with_data(mem_fs: dict[str, str]) -> None:
    """With usage + task_history present, the baseline buckets $/task by shape."""
    config = _make_config()
    config.ensure_dirs()
    mem_fs[str(config.usage_log_path)] = json.dumps({
        "iteration": 1,
        "task_id": "t1",
        "total_cost_usd": 2.0,
        "cache_read_tokens": 80,
        "input_tokens": 20,
        "per_caller": {"auto_fix": 4, "self_review": 3},
    }) + "\n"
    mem_fs[str(config.task_history_path)] = json.dumps({
        "task_id": "t1",
        "goal": "remediate the broken merge gate",
        "outcome": "success",
        "replans": 2,
    }) + "\n"

    args = argparse.Namespace()
    output = capture_stdout(cmd_burn_baseline, args, config)
    assert "| remediation | 1 | $2.0000 | $2.0000 |" in output
    assert "Prompt-cache hit rate: 80.0%" in output
    assert (
        "Implementation churn (window): "
        "auto_fix calls = 4, self_review calls = 3"
    ) in output
    assert (
        "Task quality (window): 1 task — "
        "success 1, partial 0, failure 0; "
        "replans total 2 (avg 2.00/task)"
    ) in output


def test_cmd_burn_baseline_since_arg(mem_fs: dict[str, str]) -> None:
    """``--since`` is parsed into a UTC-aware datetime passed to the generator.

    ``cmd_burn_baseline`` imports ``generate_burn_baseline`` function-locally,
    so the patch target is the source module, ``maestro.cost_tracking``.
    """
    config = _make_config()
    args = argparse.Namespace(since="2026-06-21")
    with patch(
        "maestro.cost_tracking.generate_burn_baseline", return_value="report",
    ) as mock_gen:
        capture_stdout(cmd_burn_baseline, args, config)

    mock_gen.assert_called_once()
    since = mock_gen.call_args.kwargs["since"]
    assert since.tzinfo is not None
    assert since == datetime(2026, 6, 21, tzinfo=UTC)


def test_cmd_burn_baseline_since_offset_converted_to_utc(
    mem_fs: dict[str, str],
) -> None:
    """An offset-aware ``--since`` is converted to UTC, not relabelled.

    ``datetime.fromisoformat`` accepts an explicit offset (e.g. ``+05:00``);
    an unconditional ``.replace(tzinfo=UTC)`` would discard the offset and
    shift the cutoff instant by 5 hours. The cutoff must be the same instant
    expressed in UTC: ``2026-06-21T00:00:00+05:00`` is ``2026-06-20T19:00:00Z``.
    """
    config = _make_config()
    args = argparse.Namespace(since="2026-06-21T00:00:00+05:00")
    with patch(
        "maestro.cost_tracking.generate_burn_baseline", return_value="report",
    ) as mock_gen:
        capture_stdout(cmd_burn_baseline, args, config)

    since = mock_gen.call_args.kwargs["since"]
    assert since == datetime(2026, 6, 20, 19, 0, 0, tzinfo=UTC)


def test_cmd_burn_baseline_invalid_since_prints_error(
    mem_fs: dict[str, str],
) -> None:
    """A malformed ``--since`` prints an error and never calls the generator."""
    config = _make_config()
    args = argparse.Namespace(since="not-a-date")
    with patch("maestro.cost_tracking.generate_burn_baseline") as mock_gen:
        output = capture_stdout(cmd_burn_baseline, args, config)

    assert "Invalid --since date" in output
    mock_gen.assert_not_called()


def test_cmd_burn_baseline_compare_arg(mem_fs: dict[str, str]) -> None:
    """``--compare`` is parsed to a UTC-aware cutoff routed to the comparison.

    ``cmd_burn_baseline`` imports ``generate_burn_comparison`` function-locally,
    so the patch target is the source module, ``maestro.cost_tracking``.
    """
    config = _make_config()
    args = argparse.Namespace(since=None, compare="2026-06-21")
    with patch(
        "maestro.cost_tracking.generate_burn_comparison", return_value="cmp-report",
    ) as mock_cmp:
        output = capture_stdout(cmd_burn_baseline, args, config)

    mock_cmp.assert_called_once()
    cutoff = mock_cmp.call_args.kwargs["cutoff"]
    assert cutoff.tzinfo is not None
    assert cutoff == datetime(2026, 6, 21, tzinfo=UTC)
    assert "cmp-report" in output


def test_cmd_burn_baseline_compare_offset_converted_to_utc(
    mem_fs: dict[str, str],
) -> None:
    """An offset-aware ``--compare`` cutoff is converted to the same UTC instant."""
    config = _make_config()
    args = argparse.Namespace(since=None, compare="2026-06-21T00:00:00+05:00")
    with patch(
        "maestro.cost_tracking.generate_burn_comparison", return_value="cmp",
    ) as mock_cmp:
        capture_stdout(cmd_burn_baseline, args, config)

    cutoff = mock_cmp.call_args.kwargs["cutoff"]
    assert cutoff == datetime(2026, 6, 20, 19, 0, 0, tzinfo=UTC)


def test_cmd_burn_baseline_invalid_compare_prints_error(
    mem_fs: dict[str, str],
) -> None:
    """A malformed ``--compare`` prints an error and never calls the generator."""
    config = _make_config()
    args = argparse.Namespace(since=None, compare="not-a-date")
    with patch("maestro.cost_tracking.generate_burn_comparison") as mock_cmp:
        output = capture_stdout(cmd_burn_baseline, args, config)

    assert "Invalid --compare date" in output
    mock_cmp.assert_not_called()


def test_cmd_burn_baseline_since_and_compare_mutually_exclusive() -> None:
    """Passing both ``--since`` and ``--compare`` is an argparse error (exit 2).

    The mutual-exclusion group rejects the pair at ``parse_args`` time — before
    ``Config.from_env()`` — so ``main()`` exits 2 without building a config.
    """
    from maestro.cli import main

    argv = [
        "maestro", "burn-baseline",
        "--since", "2026-06-21", "--compare", "2026-06-22",
    ]
    with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
