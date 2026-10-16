"""Tests for the owned, loseable quality-gate verdict (``maestro gate-check``).

All subprocess calls are mocked — no real processes, filesystem, or network,
per the Testing Principles. The aggregation contract is the focus: the
aggregate exit code is ``0`` if and only if every gate passed, and any failing
gate is named.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from maestro import gate_capacity, gate_environment
from maestro.config import Config
from maestro.gate_check import (
    _HEAD_READ_TIMEOUT,
    MISSING_SIBLING_CWD_EXIT_CODE,
    GateCheckResult,
    _origin_main_head,
    build_gate_commands,
    build_sibling_gate_commands,
    format_gate_check_json,
    refresh_runnability_verdict,
    run_gate_check,
)
from maestro.work_target import WorkTarget
from tests.support.gate_sites import REPO_ROOT


def _default_work_target() -> WorkTarget:
    """A :class:`WorkTarget` with the default self gate atoms (``maestro/``/``tests/``/``scripts/``).

    ``run_gate_check`` now builds its commands from a ``WorkTarget``; the
    aggregation-contract tests inject this default so the built commands are
    the CI-faithful default atoms.
    """
    return WorkTarget(
        repo_workspace=Path("."),
        github_repo=None,
        gate_source_path="maestro/",
        gate_tests_path="tests/",
        gate_lint_extra_paths=("scripts/",),
        task_framing="self_evolution",
        focus_profile="self_evolution",
    )


def _gate_config(*, oracle: bool = False) -> MagicMock:
    """A stand-in ``Config`` for ``cmd_gate_check``, oracle flag off by default.

    A bare :class:`MagicMock` answers *every* attribute truthily, so it would
    silently turn ``[loop] gate_environment_oracle_enabled`` on and make each
    dispatch test issue a runnability probe. Pinning the flag keeps these tests
    about the aggregation contract they are named for; the two that are about
    the oracle pass ``oracle=True``.
    """
    config = MagicMock()
    config.gate_environment_oracle_enabled = oracle
    return config


def _completed(returncode: int) -> subprocess.CompletedProcess[bytes]:
    """Build a stub CompletedProcess carrying only the exit code."""
    return subprocess.CompletedProcess(args=[], returncode=returncode)


# ── run_gate_check: aggregation contract ────────────────────────────────


def test_run_gate_check_all_pass_aggregate_zero() -> None:
    """All gates exit 0 → per-gate codes all 0 and aggregate 0."""
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ) as mock_run:
        result = run_gate_check(_default_work_target())

    assert result.exit_codes == {"ruff": 0, "mypy": 0, "pytest": 0}
    assert result.aggregate == 0
    assert result.failing_gates == []
    # One subprocess invocation per gate — ruff, mypy, pytest.
    assert mock_run.call_count == len(build_gate_commands(_default_work_target()))


def test_run_gate_check_invokes_each_gate_module() -> None:
    """Each gate runs as ``python -m <tool> ...`` in declared run order."""
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ) as mock_run:
        run_gate_check(_default_work_target())

    invoked_tools = [call.args[0][2] for call in mock_run.call_args_list]
    assert invoked_tools == ["ruff", "mypy", "pytest"]


def test_run_gate_check_single_gate_fail_names_gate() -> None:
    """A single failing gate → nonzero aggregate that names only that gate."""

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        # argv == [executable, "-m", tool, ...]; mypy fails, others pass.
        return _completed(2 if argv[2] == "mypy" else 0)

    with patch("maestro.gate_check.subprocess.run", side_effect=fake_run):
        result = run_gate_check(_default_work_target())

    assert result.aggregate == 2
    assert result.aggregate != 0
    assert result.failing_gates == ["mypy"]


def test_run_gate_check_multi_gate_fail_names_all_in_order() -> None:
    """Multiple failing gates → aggregate is first nonzero; all named in order."""

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        codes = {"ruff": 1, "mypy": 0, "pytest": 5}
        return _completed(codes[argv[2]])

    with patch("maestro.gate_check.subprocess.run", side_effect=fake_run):
        result = run_gate_check(_default_work_target())

    # First nonzero in run order is ruff (exit 1).
    assert result.aggregate == 1
    assert result.failing_gates == ["ruff", "pytest"]


def test_run_gate_check_passes_check_false() -> None:
    """Gates run with ``check=False`` so a nonzero exit is captured, not raised."""
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ) as mock_run:
        run_gate_check(_default_work_target())

    for call in mock_run.call_args_list:
        assert call.kwargs["check"] is False


def test_run_gate_check_routes_gate_stdout_to_stderr() -> None:
    """Each gate's stdout is redirected to the parent's stderr.

    This keeps the caller's stdout clean for the verdict so ``--json`` can emit
    a single parseable line — gate diagnostics never pollute it.
    """
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ) as mock_run:
        run_gate_check(_default_work_target())

    for call in mock_run.call_args_list:
        assert call.kwargs["stdout"] is sys.stderr


# ── build_gate_commands: owner-driven path substitution ─────────────────


def test_build_gate_commands_default_atoms_are_ci_faithful() -> None:
    """The default self atoms are CI-faithful: ``scripts/`` joins ruff/mypy, never pytest."""
    assert build_gate_commands(_default_work_target()) == (
        ("ruff", ("ruff", "check", "maestro/", "tests/", "scripts/")),
        ("mypy", ("mypy", "maestro/", "scripts/")),
        ("pytest", ("pytest", "tests/", "-q")),
    )


def test_build_gate_commands_none_lease_is_byte_identical() -> None:
    """A None lease (flag off) reproduces today's tuple exactly — no worker atoms."""
    assert build_gate_commands(_default_work_target(), lease=None) == (
        ("ruff", ("ruff", "check", "maestro/", "tests/", "scripts/")),
        ("mypy", ("mypy", "maestro/", "scripts/")),
        ("pytest", ("pytest", "tests/", "-q")),
    )


def test_build_gate_commands_flag_on_splices_worker_width() -> None:
    """A multi-worker grant splices ``-n <count> --dist loadfile`` into the pytest gate."""
    lease = gate_capacity.GateLease(workers=4, slots=8)
    assert build_gate_commands(_default_work_target(), lease=lease) == (
        ("ruff", ("ruff", "check", "maestro/", "tests/", "scripts/")),
        ("mypy", ("mypy", "maestro/", "scripts/")),
        ("pytest", ("pytest", "tests/", "-q", "-n", "4", "--dist", "loadfile")),
    )


def test_build_gate_commands_one_worker_grant_stays_serial() -> None:
    """A one-worker floor grant emits no worker atoms — the tuple stays serial."""
    lease = gate_capacity.GateLease(workers=1, slots=8)
    assert build_gate_commands(_default_work_target(), lease=lease)[2] == (
        "pytest", ("pytest", "tests/", "-q"),
    )


def test_run_gate_check_holds_one_slot_across_all_gates() -> None:
    """run_gate_check wraps the whole three-gate loop in a single lease slot.

    One acquire before ruff, one release after pytest — all three gates run inside
    the same held slot, and the granted width is spliced into the pytest gate.
    """
    events: list[str] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, runs_path: object = None, cgroup_reserve: bool = False,
        **kwargs: object,
    ) -> Iterator[gate_capacity.GateRun]:
        events.append(f"acquire:{slot_dir}")
        try:
            yield gate_capacity.GateRun(
                lease=gate_capacity.GateLease(workers=4, slots=8),
            )
        finally:
            events.append("release")

    argvs: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        events.append(f"run:{argv[2]}")
        argvs.append(argv)
        return _completed(0)

    slot_dir = Path("/fake/slots")
    with patch(
        "maestro.gate_capacity.lease_for_gate_run", fake_lease,
    ), patch("maestro.gate_check.subprocess.run", side_effect=fake_run):
        result = run_gate_check(_default_work_target(), lease_slot_dir=slot_dir)

    assert result.aggregate == 0
    # Exactly one slot held across all three gates.
    assert events == [
        f"acquire:{slot_dir}", "run:ruff", "run:mypy", "run:pytest", "release",
    ]
    # The pytest gate carries the granted width (argv[:2] is the python -m prefix).
    assert argvs[2][2:] == [
        "pytest", "tests/", "-q", "-n", "4", "--dist", "loadfile",
    ]


def test_run_gate_check_forwards_cgroup_reserve_to_the_seam() -> None:
    """The bound-aware reserve opt-in reaches ``lease_for_gate_run``'s worker plan.

    ``run_gate_check`` owns none of the policy — it only carries
    ``[loop] gate_lease_cgroup_reserve_enabled`` through, so the value the caller
    passed must arrive at the seam unchanged (and default ``False`` when omitted).
    """
    captured: list[bool] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, runs_path: object = None, cgroup_reserve: bool = False,
        **kwargs: object,
    ) -> Iterator[gate_capacity.GateRun]:
        captured.append(cgroup_reserve)
        yield gate_capacity.GateRun(lease=None)

    with patch(
        "maestro.gate_capacity.lease_for_gate_run", fake_lease,
    ), patch("maestro.gate_check.subprocess.run", return_value=_completed(0)):
        run_gate_check(_default_work_target(), lease_slot_dir=Path("/fake/slots"))
        run_gate_check(
            _default_work_target(),
            lease_slot_dir=Path("/fake/slots"),
            cgroup_reserve=True,
        )

    assert captured == [False, True]


def test_run_gate_check_forwards_admission_cap_to_the_seam() -> None:
    """``[loop] gate_lease_admission_cap_enabled`` reaches the seam unchanged."""
    captured: list[object] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, **kwargs: object,
    ) -> Iterator[gate_capacity.GateRun]:
        captured.append(kwargs["admission_cap"])
        yield gate_capacity.GateRun(lease=None)

    with patch(
        "maestro.gate_capacity.lease_for_gate_run", fake_lease,
    ), patch("maestro.gate_check.subprocess.run", return_value=_completed(0)):
        run_gate_check(_default_work_target(), lease_slot_dir=Path("/fake/slots"))
        run_gate_check(
            _default_work_target(),
            lease_slot_dir=Path("/fake/slots"),
            admission_cap=True,
        )

    assert captured == [False, True]


@pytest.mark.parametrize(
    ("rcs", "expected_outcome"),
    [((0, 0, 0), "CLEAN"), ((0, 0, 1), "FAILED")],
)
def test_run_gate_check_records_one_row_with_coarse_outcome(
    mem_fs: dict[str, str],
    rcs: tuple[int, int, int],
    expected_outcome: str,
) -> None:
    """Flag on, run_gate_check records one row; the outcome is a coarse CLEAN/FAILED.

    The real ``lease_for_gate_run`` seam runs (only the host readers are mocked),
    so the row lands via the actual recording path. CLEAN iff every gate exited
    zero, else FAILED.
    """
    runs_path = Path("/fake/data/gate_runs.jsonl")

    @contextmanager
    def fake_acquire(lock_dir: object, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "slot"

    codes = iter(rcs)

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return _completed(next(codes))

    with (
        patch(
            "maestro.gate_capacity.measure_host_capacity",
            return_value=gate_capacity.HostCapacity(cores=8, available_mib=64000),
        ),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        patch("maestro.gate_check.subprocess.run", side_effect=fake_run),
    ):
        run_gate_check(
            _default_work_target(),
            lease_slot_dir=Path("/fake/slots"),
            runs_path=runs_path,
        )

    lines = mem_fs[str(runs_path)].strip().splitlines()
    assert len(lines) == 1
    record = gate_capacity._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "slot"
    assert record.outcome == expected_outcome
    # This aggregate spends the flat selfcheck constant, never a lease-granted
    # deadline, so its rows must not read as the loop's budgeted suite runs —
    # one landing as "in_process" would reset derive_deadline's escalation.
    assert record.source == "gate_check"


def test_run_gate_check_records_no_row_when_slot_dir_none(
    mem_fs: dict[str, str],
) -> None:
    """A None slot dir (flag off) writes no row even when runs_path is given."""
    runs_path = Path("/fake/data/gate_runs.jsonl")
    with patch("maestro.gate_check.subprocess.run", side_effect=lambda *a, **k: _completed(0)):
        run_gate_check(
            _default_work_target(), lease_slot_dir=None, runs_path=runs_path,
        )
    assert str(runs_path) not in mem_fs


def test_build_gate_commands_honours_overridden_atoms() -> None:
    """A ``[work]``-overridden target swaps the source/tests paths in every gate."""
    target = WorkTarget(
        repo_workspace=Path("/ext"),
        github_repo="acme/widget",
        gate_source_path="widget/",
        gate_tests_path="widget_tests/",
        gate_lint_extra_paths=(),
        task_framing="external_delivery",
        focus_profile="external_delivery",
    )
    assert build_gate_commands(target) == (
        ("ruff", ("ruff", "check", "widget/", "widget_tests/")),
        ("mypy", ("mypy", "widget/")),
        ("pytest", ("pytest", "widget_tests/", "-q")),
    )


# ── GateCheckResult properties ──────────────────────────────────────────


def test_aggregate_zero_only_when_all_zero() -> None:
    assert GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0}).aggregate == 0


def test_aggregate_returns_first_nonzero_code() -> None:
    result = GateCheckResult({"ruff": 0, "mypy": 3, "pytest": 7})
    assert result.aggregate == 3
    assert result.failing_gates == ["mypy", "pytest"]


# ── format_gate_check_json: machine-readable shape ──────────────────────


def test_format_gate_check_json_all_pass_shape() -> None:
    """All-pass result → per-gate codes 0, aggregate 0, passed true."""
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    payload = json.loads(format_gate_check_json(result))

    assert payload == {
        "ruff": 0,
        "mypy": 0,
        "pytest": 0,
        "aggregate": 0,
        "passed": True,
    }


def test_format_gate_check_json_any_fail_shape() -> None:
    """A failing gate → nonzero aggregate, passed false, failing code present."""
    result = GateCheckResult({"ruff": 0, "mypy": 2, "pytest": 0})
    payload = json.loads(format_gate_check_json(result))

    assert payload["mypy"] == 2
    assert payload["aggregate"] == 2
    assert payload["passed"] is False


def test_format_gate_check_json_returns_str() -> None:
    """The serializer returns a JSON string, not a dict."""
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    assert isinstance(format_gate_check_json(result), str)


# ── CLI dispatch + exit-code propagation ────────────────────────────────


def test_cmd_gate_check_exits_with_aggregate_zero() -> None:
    """cmd_gate_check calls run_gate_check and exits with aggregate 0 on pass."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    config = _gate_config()
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ) as mock_check, pytest.raises(SystemExit) as exc:
        cmd_gate_check(argparse.Namespace(), config)

    # The handler resolves the gate target from the config's WorkTarget owner and
    # threads the derived lease slot dir plus the telemetry sidecar path (126).
    mock_check.assert_called_once_with(
        config.work_target.return_value,
        lease_slot_dir=config.gate_lease_dir,
        runs_path=config.gate_runs_path,
        cgroup_reserve=config.gate_lease_cgroup_reserve_enabled,
        admission_cap=config.gate_lease_admission_cap_enabled,
        full=False,
    )
    assert exc.value.code == 0


def test_cmd_gate_check_exits_with_nonzero_aggregate() -> None:
    """A failing gate propagates its nonzero aggregate to the process exit."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 2, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_check(argparse.Namespace(), _gate_config())

    assert exc.value.code == 2


def test_cmd_gate_check_prints_breakdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The handler prints a per-gate PASS/FAIL breakdown and the aggregate."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 2, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ), pytest.raises(SystemExit):
        cmd_gate_check(argparse.Namespace(), _gate_config())

    out = capsys.readouterr().out
    assert "ruff" in out
    assert "PASS" in out
    assert "FAIL (exit 2)" in out
    assert "failing: mypy" in out
    assert "aggregate exit: 2" in out


def test_cmd_gate_check_json_prints_parseable_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` prints JSON carrying the aggregate; exit equals the aggregate."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 2, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_check(argparse.Namespace(json=True), _gate_config())

    payload = json.loads(capsys.readouterr().out)
    assert payload["aggregate"] == 2
    assert payload["passed"] is False
    assert exc.value.code == 2


def test_cmd_gate_check_json_all_pass_exit_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` on an all-pass result prints passed true and exits 0."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_check(argparse.Namespace(json=True), _gate_config())

    payload = json.loads(capsys.readouterr().out)
    assert payload["aggregate"] == 0
    assert payload["passed"] is True
    assert exc.value.code == 0


def test_cmd_gate_check_json_suppresses_human_breakdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` emits only JSON — none of the human breakdown text."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ), pytest.raises(SystemExit):
        cmd_gate_check(argparse.Namespace(json=True), _gate_config())

    out = capsys.readouterr().out
    assert "Quality-gate verdict" not in out
    assert "aggregate exit:" not in out


def test_cmd_gate_check_json_stdout_is_pure_json_with_live_gate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` stdout stays parseable even when gates stream diagnostics.

    Unlike the fully-mocked ``run_gate_check`` tests, this patches only
    ``subprocess.run`` so the real routing runs: a gate that prints to the
    stdout stream it is handed must land on stderr, leaving the caller's
    stdout a single ``json.loads``-able line (no manual last-line extraction).
    """
    from maestro.cli import cmd_gate_check

    def noisy_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        # A real gate streams diagnostics to whatever stdout it was given.
        print(f"{argv[2]}: All checks passed!", file=kwargs["stdout"])
        return _completed(0)

    config = _gate_config()
    config.work_target.return_value = _default_work_target()
    # Flag off (no slot dir) so the real run_gate_check takes no slot / host read.
    config.gate_lease_dir = None
    with patch(
        "maestro.gate_check.subprocess.run", side_effect=noisy_run
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_check(argparse.Namespace(json=True), config)

    captured = capsys.readouterr()
    # stdout parses cleanly — the machine-readability guarantee actually holds.
    payload = json.loads(captured.out)
    assert payload["passed"] is True
    assert payload["aggregate"] == 0
    # Every gate's live diagnostics went to stderr, never polluting stdout.
    assert "All checks passed!" in captured.err
    assert "All checks passed!" not in captured.out
    assert exc.value.code == 0


def test_cmd_gate_check_json_single_object_failing_gate_exit_equals_aggregate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` stdout is a single JSON object and exit==aggregate when a gate fails.

    All subprocesses are mocked (``subprocess.run`` patched) and made noisy on
    both the handed stdout stream and stderr. With ruff passing, mypy failing
    (code 2), and pytest passing, stdout must still parse as exactly one JSON
    object carrying ``aggregate == 2`` / ``passed False`` — no gate diagnostics
    leaking onto stdout — and the process must exit with that same aggregate.
    """
    from maestro.cli import cmd_gate_check

    codes = {"ruff": 0, "mypy": 2, "pytest": 0}

    def noisy_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        gate = argv[2]
        # Diagnostics on both streams a real gate would write to.
        print(f"{gate}: streamed diagnostics line", file=kwargs["stdout"])
        print(f"{gate}: stderr diagnostics line", file=sys.stderr)
        return _completed(codes[gate])

    config = _gate_config()
    config.work_target.return_value = _default_work_target()
    # Flag off (no slot dir) so the real run_gate_check takes no slot / host read.
    config.gate_lease_dir = None
    with patch(
        "maestro.gate_check.subprocess.run", side_effect=noisy_run
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_check(argparse.Namespace(json=True), config)

    captured = capsys.readouterr()
    # stdout is exactly one JSON object — a single line, no surrounding noise.
    stdout_lines = captured.out.splitlines()
    assert len(stdout_lines) == 1
    payload = json.loads(stdout_lines[0])
    assert payload == {
        "ruff": 0,
        "mypy": 2,
        "pytest": 0,
        "aggregate": 2,
        "passed": False,
    }
    # No subprocess diagnostics polluted stdout; they all went to stderr.
    assert "diagnostics" not in captured.out
    assert "streamed diagnostics line" in captured.err
    assert "stderr diagnostics line" in captured.err
    # Process exit equals the aggregate verdict.
    assert exc.value.code == 2
    assert exc.value.code == payload["aggregate"]


def test_gate_check_json_flag_parsed_and_dispatched(
    mem_fs: dict[str, str],
) -> None:
    """``maestro gate-check --json`` parses ``json=True`` and reaches the handler."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "gate-check", "--json"]), patch(
        "maestro.cli.cmd_gate_check"
    ) as mock_cmd:
        main()

    mock_cmd.assert_called_once()
    dispatched_args = mock_cmd.call_args.args[0]
    assert dispatched_args.json is True


def test_gate_check_json_flag_defaults_false(
    mem_fs: dict[str, str],
) -> None:
    """Without ``--json`` the gate-check handler receives ``json=False``."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "gate-check"]), patch(
        "maestro.cli.cmd_gate_check"
    ) as mock_cmd:
        main()

    mock_cmd.assert_called_once()
    dispatched_args = mock_cmd.call_args.args[0]
    assert dispatched_args.json is False


def test_gate_check_registered_in_cli_handlers() -> None:
    """``gate-check`` is wired into main()'s handler dispatch table."""
    import inspect

    from maestro import cli

    source = inspect.getsource(cli.main)
    assert '"gate-check": cmd_gate_check' in source


# ── --full: the sibling-package gate set (issue #3655) ──────────────────


def test_build_sibling_gate_commands_mirrors_the_ci_package_quality_job() -> None:
    """The six sibling entries name both packages with their CI cwd.

    CI's ``package-quality`` job runs three ``maestro_review`` gates from the
    repo root and three ``maestro_sdk`` gates under ``working-directory:
    maestro_sdk``; the local mirror must carry that split, or a green here is
    still not CI-faithful.
    """
    entries = build_sibling_gate_commands()

    assert [name for name, _argv, _cwd in entries] == [
        "ruff:maestro_review",
        "mypy:maestro_review",
        "pytest:maestro_review",
        "ruff:maestro_sdk",
        "mypy:maestro_sdk",
        "pytest:maestro_sdk",
    ]
    assert [cwd for _name, _argv, cwd in entries] == [
        None, None, None, "maestro_sdk", "maestro_sdk", "maestro_sdk",
    ]
    assert [argv[0] for _name, argv, _cwd in entries] == [
        "ruff", "mypy", "pytest", "ruff", "mypy", "pytest",
    ]


def test_run_gate_check_full_invokes_all_nine_gates() -> None:
    """``full=True`` fans out to the three maestro gates plus the six sibling ones."""
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ) as mock_run:
        result = run_gate_check(_default_work_target(), full=True)

    assert list(result.exit_codes) == [
        "ruff",
        "mypy",
        "pytest",
        "ruff:maestro_review",
        "mypy:maestro_review",
        "pytest:maestro_review",
        "ruff:maestro_sdk",
        "mypy:maestro_sdk",
        "pytest:maestro_sdk",
    ]
    assert mock_run.call_count == 9
    assert result.aggregate == 0


def test_run_gate_check_full_passes_each_siblings_cwd() -> None:
    """The three ``maestro_sdk`` gates run under that directory; the rest at root."""
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ) as mock_run:
        run_gate_check(_default_work_target(), full=True)

    sibling_calls = mock_run.call_args_list[3:]
    assert [call.kwargs["cwd"] for call in sibling_calls] == [
        None, None, None, "maestro_sdk", "maestro_sdk", "maestro_sdk",
    ]


def test_run_gate_check_full_sibling_failure_alone_drives_aggregate() -> None:
    """A red sibling gate with all maestro gates green still fails the aggregate.

    This is the bad state the flag exists to remove: before ``--full`` the
    sibling gates never ran, so this exact tree reported a clean local green
    and then failed CI's ``package-quality`` job.
    """

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        # Only the maestro_sdk pytest gate is red.
        if kwargs.get("cwd") == "maestro_sdk" and argv[2] == "pytest":
            return _completed(1)
        return _completed(0)

    with patch("maestro.gate_check.subprocess.run", side_effect=fake_run):
        result = run_gate_check(_default_work_target(), full=True)

    assert result.aggregate == 1
    assert result.failing_gates == ["pytest:maestro_sdk"]


def test_run_gate_check_full_missing_sibling_cwd_is_a_red_gate_not_a_crash() -> None:
    """A missing ``maestro_sdk`` cwd records a red gate instead of raising.

    ``subprocess.run`` raises ``FileNotFoundError`` before the child starts when
    its ``cwd`` does not exist — which is what ``--full`` hits when gate-check is
    invoked from anywhere but the repo root. The maestro triple degrades to clean
    nonzero exits there, so the sibling set must too: a verdict, not a traceback.
    """

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if kwargs.get("cwd") == "maestro_sdk":
            raise FileNotFoundError(2, "No such file or directory: 'maestro_sdk'")
        return _completed(0)

    with patch("maestro.gate_check.subprocess.run", side_effect=fake_run):
        result = run_gate_check(_default_work_target(), full=True)

    assert result.exit_codes["pytest:maestro_sdk"] == MISSING_SIBLING_CWD_EXIT_CODE
    assert result.aggregate == MISSING_SIBLING_CWD_EXIT_CODE
    assert result.failing_gates == [
        "ruff:maestro_sdk", "mypy:maestro_sdk", "pytest:maestro_sdk",
    ]


def test_run_gate_check_full_maestro_failure_wins_the_aggregate_code() -> None:
    """With both sets red the earlier maestro gate's code is the aggregate."""

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if kwargs.get("cwd") is None and argv[2] == "mypy":
            return _completed(2)
        if kwargs.get("cwd") == "maestro_sdk":
            return _completed(1)
        return _completed(0)

    with patch("maestro.gate_check.subprocess.run", side_effect=fake_run):
        result = run_gate_check(_default_work_target(), full=True)

    # ``aggregate`` returns the first nonzero in insertion order, and the
    # maestro triple is inserted first — so the sibling set widens the verdict
    # without displacing the maestro gates' more specific code.
    assert result.aggregate == 2
    assert result.failing_gates[0] == "mypy"
    assert "ruff:maestro_sdk" in result.failing_gates


def test_run_gate_check_full_widens_the_json_payload() -> None:
    """``format_gate_check_json`` serializes the sibling codes with no change."""
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ):
        result = run_gate_check(_default_work_target(), full=True)

    payload = json.loads(format_gate_check_json(result))
    assert payload["pytest:maestro_sdk"] == 0
    assert payload["ruff:maestro_review"] == 0
    assert payload["passed"] is True


def test_run_gate_check_full_false_is_byte_identical_to_today() -> None:
    """Without ``full`` exactly the three maestro commands run, unchanged."""
    with patch(
        "maestro.gate_check.subprocess.run",
        return_value=_completed(0),
    ) as mock_run:
        result = run_gate_check(_default_work_target())

    assert list(result.exit_codes) == ["ruff", "mypy", "pytest"]
    assert mock_run.call_count == 3
    invoked = [tuple(call.args[0]) for call in mock_run.call_args_list]
    assert invoked == [
        (sys.executable, "-m", *argv)
        for _name, argv in build_gate_commands(_default_work_target())
    ]


def test_run_gate_check_full_holds_one_slot_for_all_nine_gates() -> None:
    """The sibling gates run inside the same lease slot, not a second one."""
    entered = 0

    @contextmanager
    def fake_lease(*_args: object, **_kwargs: object) -> Iterator[MagicMock]:
        nonlocal entered
        entered += 1
        run = MagicMock()
        run.lease = None
        yield run

    with patch(
        "maestro.gate_check.subprocess.run", return_value=_completed(0),
    ), patch("maestro.gate_check.gate_capacity.lease_for_gate_run", fake_lease):
        run_gate_check(_default_work_target(), full=True)

    assert entered == 1


def test_cmd_gate_check_full_routes_the_flag() -> None:
    """The handler forwards ``--full`` into ``run_gate_check``."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ) as mock_run, pytest.raises(SystemExit):
        cmd_gate_check(argparse.Namespace(full=True), _gate_config())

    assert mock_run.call_args.kwargs["full"] is True


def test_cmd_gate_check_full_defaults_false_when_absent() -> None:
    """A namespace without ``full`` still routes ``full=False``."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ) as mock_run, pytest.raises(SystemExit):
        cmd_gate_check(argparse.Namespace(), _gate_config())

    assert mock_run.call_args.kwargs["full"] is False


def test_cmd_gate_check_breakdown_aligns_package_qualified_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The breakdown column is wide enough for the sibling gate names."""
    from maestro.cli import cmd_gate_check

    result = GateCheckResult({"ruff": 0, "pytest:maestro_review": 1})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result
    ), pytest.raises(SystemExit):
        cmd_gate_check(argparse.Namespace(full=True), _gate_config())

    out = capsys.readouterr().out
    assert "  pytest:maestro_review  FAIL (exit 1)" in out
    assert "failing: pytest:maestro_review" in out


def test_gate_check_full_flag_parsed_and_dispatched(
    mem_fs: dict[str, str],
) -> None:
    """``maestro gate-check --full`` parses ``full=True`` and reaches the handler."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "gate-check", "--full"]), patch(
        "maestro.cli.cmd_gate_check"
    ) as mock_cmd:
        main()

    mock_cmd.assert_called_once()
    assert mock_cmd.call_args.args[0].full is True


def test_gate_check_full_flag_defaults_false(
    mem_fs: dict[str, str],
) -> None:
    """Without ``--full`` the gate-check handler receives ``full=False``."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "gate-check"]), patch(
        "maestro.cli.cmd_gate_check"
    ) as mock_cmd:
        main()

    mock_cmd.assert_called_once()
    assert mock_cmd.call_args.args[0].full is False


def _package_quality_gate_steps() -> list[tuple[str, list[str], str | None]]:
    """Parse CI's ``package-quality`` job into normalized ``(tool, args, cwd)`` rows.

    Reads ``.github/workflows/quality-gates.yml`` — the authority this mirror
    exists to track — and keeps every step whose ``run:`` invokes one of the
    three gate tools. Two purely cosmetic differences are normalized away so the
    comparison tracks the *gate*, not its spelling: a trailing ``/`` on a path
    atom (``tests/`` vs ``tests``), and the output-verbosity flags ``-v`` / ``-q``
    (CI is verbose, the local aggregate is quiet, and neither changes what runs).
    Everything else — tool, path atoms, ``--no-cov``, order, and the
    ``working-directory`` each step runs under — is compared verbatim.
    """
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/quality-gates.yml").read_text(encoding="utf-8")
    )
    rows: list[tuple[str, list[str], str | None]] = []
    for step in workflow["jobs"]["package-quality"]["steps"]:
        tokens = str(step.get("run", "")).split()
        if not tokens or tokens[0] not in {"ruff", "mypy", "pytest"}:
            continue
        rows.append((tokens[0], _normalize_gate_args(tokens[1:]), step.get("working-directory")))
    return rows


def _quality_gate_steps() -> list[tuple[str, list[str]]]:
    """Parse the ruff/mypy steps of CI's maestro ``quality`` job into normalized rows.

    Same normalization as :func:`_package_quality_gate_steps`; pytest is left
    out because the local aggregate deliberately runs it without coverage.
    """
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/quality-gates.yml").read_text(encoding="utf-8")
    )
    rows: list[tuple[str, list[str]]] = []
    for step in workflow["jobs"]["quality"]["steps"]:
        tokens = str(step.get("run", "")).split()
        if not tokens or tokens[0] not in {"ruff", "mypy"}:
            continue
        rows.append((tokens[0], _normalize_gate_args(tokens[1:])))
    return rows


@pytest.mark.timeout(15)
def test_gate_commands_match_the_ci_quality_job() -> None:
    """The default ruff/mypy gates cover exactly the paths CI's ``quality`` job lints.

    A path CI lints but the local gate skips (``scripts/``, #4128) lets a loop
    task pass every local gate and fail only in CI; this reads the workflow so
    that drift fails here.
    """
    ci_rows = _quality_gate_steps()
    target = Config().work_target()
    local_rows = [
        (argv[0], _normalize_gate_args(argv[1:]))
        for _name, argv in build_gate_commands(target)
        if argv[0] in {"ruff", "mypy"}
    ]

    assert len(ci_rows) == 2
    assert local_rows == ci_rows


def _normalize_gate_args(args: list[str] | tuple[str, ...]) -> list[str]:
    """Strip trailing path slashes and the ``-v``/``-q`` verbosity flags from *args*."""
    return [arg.rstrip("/") for arg in args if arg not in {"-v", "-q"}]


@pytest.mark.timeout(15)
def test_sibling_gate_commands_match_the_ci_package_quality_job() -> None:
    """The local sibling mirror is pinned to the CI job it claims to reproduce.

    ``--full`` exists for one reason: a local green must mean CI's
    ``package-quality`` job is green too. Pinning the six literals to themselves
    would let a workflow edit — a new gate step, a dropped one, a changed path
    atom or ``working-directory`` — silently desync the mirror while every test
    still passed. This guard reads the workflow itself, so that edit fails here.
    """
    ci_rows = _package_quality_gate_steps()
    local_rows = [
        (argv[0], _normalize_gate_args(argv[1:]), cwd)
        for _name, argv, cwd in build_sibling_gate_commands()
    ]

    # Non-vacuity: a renamed job or a parser drift must not empty the CI side
    # and make the comparison below trivially true.
    assert len(ci_rows) == 6
    assert local_rows == ci_rows


def test_package_quality_parser_detects_a_desynced_mirror() -> None:
    """The normalizer keeps a real divergence visible (non-vacuity self-test).

    Slash/verbosity normalization is deliberately lossy; this pins how far. A
    changed path atom, a dropped ``--no-cov``, and a changed
    ``working-directory`` each still compare unequal.
    """
    assert _normalize_gate_args(["check", "src/", "tests/", "-v"]) == ["check", "src", "tests"]
    assert _normalize_gate_args(["tests", "--no-cov"]) != _normalize_gate_args(["tests"])
    assert _normalize_gate_args(["maestro_review"]) != _normalize_gate_args(["maestro_sdk"])


# ── refresh_runnability_verdict: the agent-free evidence assembly (133 step 9) ──


def _capture_refresh() -> Any:
    """Patch the oracle's ``refresh_verdict`` and record its keyword evidence."""
    return patch(
        "maestro.gate_check.gate_environment.refresh_verdict",
        return_value=gate_environment.GateRunnabilityVerdict(
            kind="SOUND", fingerprint="f",
        ),
    )


@contextmanager
def _stub_capacity(
    readings: list[object], workers: int, deadline: int,
) -> Iterator[MagicMock]:
    """Pin the readings the refresher assembles, and the fetch-free head read.

    ``_origin_main_head`` shells out to ``git``, which the autouse
    ``_block_external_commands`` fixture forbids in a unit test; the tests that
    are *about* that read patch ``subprocess.run`` directly instead.
    """
    with (
        patch.object(
            gate_capacity, "read_gate_runs", return_value=readings,
        ),
        patch.object(
            gate_capacity, "uncontended_width", return_value=workers,
        ),
        patch.object(
            gate_capacity, "derive_deadline", return_value=deadline,
        ) as mock_deadline,
        patch("maestro.gate_check._origin_main_head", return_value="head-sha"),
    ):
        yield mock_deadline


def test_refresh_runnability_verdict_assembles_the_capacity_evidence(
    mem_fs: dict[str, str],
) -> None:
    """The rows, width and deadline come from the owners that already hold them.

    Budget-lease flag on, so the derived deadline is the budget the gate run
    actually spends and ``selfcheck_max`` is the escalation cap the fit reading
    is judged against (proposal 134).
    """
    readings: list[object] = [object()]
    with _stub_capacity(readings, 4, 720) as mock_deadline, _capture_refresh() as mock_refresh:
        verdict = refresh_runnability_verdict(
            _default_work_target(),
            verdict_path=Path("/fake/data/gate_environment.json"),
            runs_path=Path("/fake/data/gate_runs.jsonl"),
            selfcheck=900,
            selfcheck_max=1350,
            budget_lease_owned=True,
            cgroup_reserve=False,
        )

    assert verdict.kind == "SOUND"
    mock_deadline.assert_called_once_with(
        readings, workers=4, selfcheck=900, selfcheck_max=1350,
    )
    kwargs = mock_refresh.call_args.kwargs
    assert kwargs["readings"] is readings
    assert kwargs["workers"] == 4
    assert kwargs["deadline_seconds"] == 720
    assert kwargs["cap_seconds"] == 1350
    assert kwargs["head"] == "head-sha"
    assert mock_refresh.call_args.args[0] == Path(
        "/fake/data/gate_environment.json",
    )


def test_refresh_runnability_verdict_judges_the_enforced_budget_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Budget-lease flag off: ``selfcheck``, no cap, and no derivation spent.

    Off, ``_run_gate_command`` kills every gate run at ``selfcheck``, so that is
    the ceiling the fit reading must be judged against — measuring a
    killed-at-900s suite against a never-enforced derived budget would read a
    budget no run ever had (proposal 134).
    """
    readings: list[object] = [object()]
    with _stub_capacity(readings, 4, 720) as mock_deadline, _capture_refresh() as mock_refresh:
        refresh_runnability_verdict(
            _default_work_target(),
            verdict_path=Path("/fake/data/gate_environment.json"),
            runs_path=Path("/fake/data/gate_runs.jsonl"),
            selfcheck=900,
            selfcheck_max=1350,
            budget_lease_owned=False,
            cgroup_reserve=False,
        )

    mock_deadline.assert_not_called()
    kwargs = mock_refresh.call_args.kwargs
    assert kwargs["deadline_seconds"] == 900
    assert kwargs["cap_seconds"] == 0


@pytest.mark.parametrize("cgroup_reserve", [True, False])
def test_refresh_runnability_verdict_plans_the_width_uncontended(
    cgroup_reserve: bool,
) -> None:
    """The width is the one the admission seam would grant now, under the same
    ``cgroup_reserve`` setting — so the oracle judges rows real runs record."""
    with (
        _stub_capacity([], 1, 900),
        patch.object(gate_capacity, "uncontended_width", return_value=1) as mock_width,
        _capture_refresh(),
    ):
        refresh_runnability_verdict(
            _default_work_target(),
            verdict_path=Path("/fake/data/gate_environment.json"),
            runs_path=Path("/fake/data/gate_runs.jsonl"),
            selfcheck=900,
            selfcheck_max=1350,
            budget_lease_owned=True,
            cgroup_reserve=cgroup_reserve,
        )

    assert mock_width.call_args.kwargs == {"cgroup_reserve": cgroup_reserve}


def test_refresh_runnability_verdict_key_cannot_collide_with_the_loop() -> None:
    """The key is prefixed, and distinct per invocation, so every call probes."""
    keys: list[str] = []
    with (
        _stub_capacity([], 1, 900),
        patch(
            "maestro.gate_check.gate_environment.refresh_verdict",
            side_effect=lambda path, **kw: keys.append(kw["iteration_key"])
            or gate_environment.GateRunnabilityVerdict(kind="SOUND", fingerprint="f"),
        ),
        patch("maestro.gate_check.datetime") as mock_dt,
    ):
        mock_dt.now.side_effect = [
            datetime(2026, 9, 21, 16, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 16, 5, 0, tzinfo=UTC),
        ]
        for _ in range(2):
            refresh_runnability_verdict(
                _default_work_target(),
                verdict_path=Path("/fake/data/gate_environment.json"),
                runs_path=Path("/fake/data/gate_runs.jsonl"),
                selfcheck=900,
                selfcheck_max=1350,
                budget_lease_owned=True,
                cgroup_reserve=False,
            )

    assert all(key.startswith("gate-check:") for key in keys)
    assert keys[0] != keys[1]
    # A bare integer is the loop's key spelling; a prefixed one can never be one.
    assert not any(key.isdigit() for key in keys)


def _collector() -> Any:
    """Assemble the refresher, then return the collector it handed the oracle."""
    with _stub_capacity([], 1, 900), _capture_refresh() as mock_refresh:
        refresh_runnability_verdict(
            _default_work_target(),
            verdict_path=Path("/fake/data/gate_environment.json"),
            runs_path=Path("/fake/data/gate_runs.jsonl"),
            selfcheck=900,
            selfcheck_max=1350,
            budget_lease_owned=True,
            cgroup_reserve=False,
        )
    return mock_refresh.call_args.kwargs["collector"]


def test_collector_returns_a_finished_run_as_the_triple() -> None:
    """A completed collection run narrows to ``(returncode, stdout, stderr)``."""
    collect = _collector()
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="5 tests collected", stderr="",
    )

    with patch("maestro.gate_check.subprocess.run", return_value=completed) as mock_run:
        assert collect() == (0, "5 tests collected", "")

    argv = mock_run.call_args.args[0]
    assert argv[:3] == [sys.executable, "-m", "pytest"]
    # The tests path atom comes from the WorkTarget owner, never a literal.
    assert argv[3] == _default_work_target().gate_tests_path
    assert "--collect-only" in argv
    assert argv[-2:] == ["-p", "no:cacheprovider"]
    assert mock_run.call_args.kwargs["timeout"] == 900


def test_collector_reports_a_killed_run_as_the_timeout_non_answer() -> None:
    """The deadline killing collection is the oracle's bare ``TIMEOUT`` literal."""
    collect = _collector()

    with patch(
        "maestro.gate_check.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=900),
    ):
        assert collect() == "TIMEOUT"


@pytest.mark.parametrize("exc", [FileNotFoundError("no python"), ValueError("bad argv")])
def test_collector_reports_a_runner_that_cannot_start_as_unavailable(
    exc: Exception,
) -> None:
    """A runner that never starts is ``UNAVAILABLE``, not an unhandled crash."""
    collect = _collector()

    with patch("maestro.gate_check.subprocess.run", side_effect=exc):
        assert collect() == "UNAVAILABLE"


def test_head_is_read_fetch_free_from_the_remote_tracking_ref() -> None:
    """The ``origin/main`` head is a plain ``rev-parse`` — no network."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="abc123\n", stderr="",
    )

    with patch("maestro.gate_check.subprocess.run", return_value=completed) as mock_run:
        assert _origin_main_head(_default_work_target()) == "abc123"

    assert mock_run.call_args.args[0] == ["git", "rev-parse", "origin/main"]
    # Bounded: a wedged git must not hang ``maestro gate-check`` forever for a
    # fact whose absence costs at most one extra probe.
    assert mock_run.call_args.kwargs["timeout"] == _HEAD_READ_TIMEOUT


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="bad"),
        FileNotFoundError("no git"),
        subprocess.TimeoutExpired(cmd=["git"], timeout=_HEAD_READ_TIMEOUT),
    ],
)
def test_head_degrades_to_empty_rather_than_crashing(outcome: object) -> None:
    """An unfetched ref, a missing git or a wedged one costs one extra probe.

    Never a crash and never a hang: the read is bounded, and the deadline
    expiring is just another empty-string shape.
    """
    kwargs = (
        {"side_effect": outcome}
        if isinstance(outcome, Exception)
        else {"return_value": outcome}
    )

    with patch("maestro.gate_check.subprocess.run", **kwargs):
        assert _origin_main_head(_default_work_target()) == ""


def test_format_gate_check_json_carries_the_runnability_kind() -> None:
    """A supplied verdict adds exactly one key; the default shape is unchanged."""
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE", fingerprint="f",
    )

    with_verdict = json.loads(format_gate_check_json(result, runnability=verdict))
    without = json.loads(format_gate_check_json(result))

    assert with_verdict["runnability"] == "UNCOLLECTABLE"
    assert "runnability" not in without
    assert with_verdict.keys() - without.keys() == {"runnability"}
