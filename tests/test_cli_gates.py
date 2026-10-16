"""gate-classify, gate-lease, gate-check and gate-scope; split out of
``tests/test_cli.py``.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro import gate_capacity, gate_environment
from maestro.cli import (
    _forward_gate_stream,
    _gate_child_outcome,
    _run_gate_child,
    cmd_gate_check,
    cmd_gate_classify,
    cmd_gate_lease,
    cmd_gate_scope,
    cmd_measure_planning_grounding,
)
from maestro.config import Config
from maestro.memory import (
    Memory,
)
from tests.conftest import capture_stdout
from tests.test_cli import (
    _SIBLINGS_RESOLVED,
    FAKE_DIR,
    _doa_cli_plan,
    _make_config,
    _sibling_finder,
)

# ── gate-classify (proposal 126) ────────────────────────────────────


def _classify(monkeypatch: pytest.MonkeyPatch, stdin_text: str, **ns: object) -> str:
    """Drive cmd_gate_classify over seeded stdin, returning the printed token."""
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    args = argparse.Namespace(**ns)
    return capture_stdout(cmd_gate_classify, args, _make_config()).strip()


def test_gate_classify_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pytest-cov shortfall line classifies as COVERAGE."""
    token = _classify(
        monkeypatch,
        "FAIL Required test coverage of 100% not reached. Total coverage: 99.50%\n",
        tool="tests", returncode=1, kind=None,
    )
    assert token == "COVERAGE"


def test_gate_classify_xdist_worker_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OOM-killed xdist worker's crash row classifies as the retryable UNAVAILABLE."""
    token = _classify(
        monkeypatch,
        "=== short test summary info ===\n"
        "FAILED tests/test_big.py::test_heavy - "
        "worker 'gw2' crashed while running 'tests/test_big.py::test_heavy'\n"
        "FAIL Required test coverage of 100% not reached. Total coverage: 97.41%\n",
        tool="tests", returncode=1, kind=None,
    )
    assert token == "UNAVAILABLE"


def test_gate_classify_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pytest FAILED summary row classifies as FAILED."""
    token = _classify(
        monkeypatch,
        "FAILED tests/test_foo.py::test_bar - AssertionError: nope\n",
        tool="tests", returncode=1, kind=None,
    )
    assert token == "FAILED"


def test_gate_classify_collection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pytest ERROR row classifies as COLLECTION_ERROR."""
    token = _classify(
        monkeypatch,
        "ERROR tests/test_foo.py - ImportError: no module named x\n",
        tool="tests", returncode=1, kind=None,
    )
    assert token == "COLLECTION_ERROR"


def test_gate_classify_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero return code classifies as CLEAN regardless of stdout content."""
    token = _classify(
        monkeypatch, "everything passed\n", tool="tests", returncode=0, kind=None,
    )
    assert token == "CLEAN"


def test_gate_classify_timeout_non_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """--kind TIMEOUT classifies the run as the TIMEOUT non-answer."""
    token = _classify(
        monkeypatch,
        "partial output before the runner was killed\n",
        tool="tests", returncode=1, kind="TIMEOUT",
    )
    assert token == "TIMEOUT"


def test_gate_classify_unavailable_non_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """--kind UNAVAILABLE classifies the run as the UNAVAILABLE non-answer.

    The gate-lease wrapper surfaces 127 when the child binary is not on PATH; the
    shell runner (``scripts/quality-gates.sh``'s ``classify_gate_run``) maps that
    status to ``--kind UNAVAILABLE``, which the classifier treats as a resource
    artifact worth one retry (proposal 126, step 12).
    """
    token = _classify(
        monkeypatch,
        "gate-lease: command not found: maestro\n",
        tool="tests", returncode=127, kind="UNAVAILABLE",
    )
    assert token == "UNAVAILABLE"


def test_gate_classify_always_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classification is the output, never the exit status: a red run still exits 0."""
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("FAILED tests/test_x.py::t - boom\n"),
    )
    args = argparse.Namespace(tool="tests", returncode=1, kind=None)
    with pytest.raises(SystemExit) as exc:
        cmd_gate_classify(args, _make_config())
    assert exc.value.code == 0


def test_gate_classify_registered_in_handlers() -> None:
    """The gate-classify subcommand is wired into the parser and handler map."""
    with patch.object(
        sys, "argv",
        ["maestro", "gate-classify", "--tool", "tests", "--returncode", "0"],
    ), patch("maestro.cli.cmd_gate_classify") as mock_handler, patch(
        "maestro.cli.Config.from_env", return_value=_make_config(),
    ):
        from maestro.cli import main
        main()
    mock_handler.assert_called_once()


# ── gate-lease (proposal 126) ───────────────────────────────────────


def test_gate_lease_enabled_probe_flag_on_exits_zero() -> None:
    """--enabled exits 0 when the flag is on, and acquires no slot."""
    args = argparse.Namespace(enabled=True, lease_command=[])
    with patch("maestro.gate_capacity.acquire_slot") as mock_acq, \
            pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, _make_config(gate_lease_enabled=True))
    assert exc.value.code == 0
    mock_acq.assert_not_called()


def test_gate_lease_enabled_probe_flag_off_exits_nonzero() -> None:
    """--enabled exits non-zero when the flag is off, and acquires no slot."""
    args = argparse.Namespace(enabled=True, lease_command=[])
    with patch("maestro.gate_capacity.acquire_slot") as mock_acq, \
            pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, _make_config(gate_lease_enabled=False))
    assert exc.value.code != 0
    mock_acq.assert_not_called()


def test_gate_lease_wrapper_forwards_config_cgroup_reserve_to_the_seam() -> None:
    """The wrapper carries ``gate_lease_cgroup_reserve_enabled`` into the seam.

    ``cmd_gate_lease`` owns none of the reserve policy — it only threads the
    config flag through, so the seam must receive exactly the configured value
    (and the default ``False`` when the flag is off).
    """
    captured: list[bool] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, runs_path: object = None, cgroup_reserve: bool = False,
        **kwargs: object,
    ) -> Any:
        captured.append(cgroup_reserve)
        yield gate_capacity.GateRun(lease=None)

    args = argparse.Namespace(enabled=False, lease_command=["true"])
    for flag in (False, True):
        config = _make_config(
            gate_lease_enabled=True, gate_lease_cgroup_reserve_enabled=flag,
        )
        with patch(
            "maestro.gate_capacity.lease_for_gate_run", fake_lease,
        ), patch(
            "maestro.cli.subprocess.Popen", return_value=_fake_child(0),
        ), pytest.raises(SystemExit):
            cmd_gate_lease(args, config)

    assert captured == [False, True]


def test_gate_lease_wrapper_forwards_config_admission_cap_to_the_seam() -> None:
    """The wrapper carries ``gate_lease_admission_cap_enabled`` into the seam."""
    captured: list[object] = []

    @contextmanager
    def fake_lease(slot_dir: object, **kwargs: object) -> Any:
        captured.append(kwargs["admission_cap"])
        yield gate_capacity.GateRun(lease=None)

    args = argparse.Namespace(enabled=False, lease_command=["true"])
    for flag in (False, True):
        config = _make_config(
            gate_lease_enabled=True, gate_lease_admission_cap_enabled=flag,
        )
        with patch(
            "maestro.gate_capacity.lease_for_gate_run", fake_lease,
        ), patch(
            "maestro.cli.subprocess.Popen", return_value=_fake_child(0),
        ), pytest.raises(SystemExit):
            cmd_gate_lease(args, config)

    assert captured == [False, True]


def test_gate_lease_wrapper_stamps_the_wrapper_source_on_its_row() -> None:
    """A hand run names itself ``"wrapper"`` so it cannot feed ``derive_deadline``.

    The wrapper enforces no deadline and runs whatever argv the caller gave, so its
    wall time is not evidence about what the in-process suite needs; the source is
    what keeps it out of the derivation's input.
    """
    captured: list[object] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, runs_path: object = None, cgroup_reserve: bool = False,
        source: object = "in_process", **kwargs: object,
    ) -> Any:
        captured.append(source)
        yield gate_capacity.GateRun(lease=None)

    args = argparse.Namespace(enabled=False, lease_command=["true"])
    with patch(
        "maestro.gate_capacity.lease_for_gate_run", fake_lease,
    ), patch(
        "maestro.cli.subprocess.Popen", return_value=_fake_child(0),
    ), pytest.raises(SystemExit):
        cmd_gate_lease(args, _make_config(gate_lease_enabled=True))

    assert captured == ["wrapper"]


def test_gate_lease_wrapper_passes_no_verdict_path_and_is_never_declined(
    mem_fs: dict[str, str],
) -> None:
    """A hand run is never refused by the runnability oracle (proposal 133).

    The wrapper is the operator's tool for *proving* a repair, so it must reach
    the gate even while the cached verdict says the environment cannot answer:
    it passes no ``verdict_path``, so the seam reads no verdict at all.
    """
    config = _make_config(
        gate_lease_enabled=True, gate_environment_oracle_enabled=True,
    )
    # A declining row on disk the wrapper must ignore.
    mem_fs[str(config.gate_environment_path)] = json.dumps({
        "kind": "UNCOLLECTABLE",
        "fingerprint": "fp",
        "identities": ["tests:COLLECTION_ERROR"],
        "detail": "collection COLLECTION_ERROR",
        "measured_at": "2026-09-21T00:00:00+00:00",
        "iteration_key": "iter-1",
    })
    captured: list[object] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, verdict_path: object = None, **kwargs: object,
    ) -> Any:
        captured.append(verdict_path)
        yield gate_capacity.GateRun(lease=None)

    args = argparse.Namespace(enabled=False, lease_command=["true"])
    with patch(
        "maestro.gate_capacity.lease_for_gate_run", fake_lease,
    ), patch(
        "maestro.cli.subprocess.Popen", return_value=_fake_child(0),
    ), pytest.raises(SystemExit):
        cmd_gate_lease(args, config)

    assert captured == [None]


def test_gate_lease_wrapper_runs_under_a_real_seam_with_a_declining_verdict(
    mem_fs: dict[str, str],
) -> None:
    """Against the real seam body a declining row still admits the hand run."""
    config = _make_config(
        gate_lease_enabled=True, gate_environment_oracle_enabled=True,
    )
    mem_fs[str(config.gate_environment_path)] = json.dumps({
        "kind": "UNCOLLECTABLE",
        "fingerprint": "fp",
        "identities": ["tests:COLLECTION_ERROR"],
        "detail": "collection COLLECTION_ERROR",
        "measured_at": "2026-09-21T00:00:00+00:00",
        "iteration_key": "iter-1",
    })
    capacity = gate_capacity.HostCapacity(cores=8, available_mib=64000)

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Any:
        yield "slot"

    args = argparse.Namespace(enabled=False, lease_command=["true"])
    with patch(
        "maestro.gate_capacity.measure_host_capacity", return_value=capacity,
    ), patch(
        "maestro.gate_capacity.acquire_slot", fake_acquire,
    ), patch(
        "maestro.gate_capacity.count_in_flight", return_value=1,
    ), patch(
        "maestro.cli.subprocess.Popen", return_value=_fake_child(0),
    ) as mock_run, pytest.raises(SystemExit):
        cmd_gate_lease(args, config)

    mock_run.assert_called_once()
    row = json.loads(mem_fs[str(config.gate_runs_path)].strip())
    assert row["admission"] == "slot"


def _fake_child(
    returncode: int, stdout: str = "", stderr: str = "",
) -> MagicMock:
    """A stand-in ``Popen`` child with canned piped streams and exit status."""
    child = MagicMock()
    child.stdout = io.StringIO(stdout)
    child.stderr = io.StringIO(stderr)
    child.wait.return_value = returncode
    return child


def _run_lease_wrapper(
    command: list[str],
    admission: str,
    returncode: int,
    workers: int = 4,
    slots: int = 4,
) -> tuple[int, list[str], list[str]]:
    """Drive cmd_gate_lease's flag-on wrapper with the lease machinery mocked.

    Returns ``(exit_code, events, run_argv)`` — the child's propagated exit code,
    the acquire/run/release ordering, and the resolved argv ``subprocess.run`` saw.
    Asserts the slot seams are handed the canonical host-scoped
    ``gate_lease_slot_dir`` lock dir and the planned slot budget (never, e.g.,
    ``memory_dir`` itself, which would collide with real state files), and that
    ``count_in_flight`` runs only *after* admission (so it sees true concurrency)
    — never on fail-open.
    """
    config = _make_config(gate_lease_enabled=True)
    expected_slot_dir = config.gate_lease_slot_dir
    events: list[str] = []
    run_argv: list[list[str]] = []
    acquire_calls: list[tuple[object, int]] = []

    @contextmanager
    def fake_acquire(lock_dir: object, slots_arg: int, wait_seconds: float = 0.0) -> Any:
        acquire_calls.append((lock_dir, slots_arg))
        events.append("acquire")
        try:
            yield admission
        finally:
            events.append("release")

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        events.append("run")
        run_argv.append(argv)
        return _fake_child(returncode)

    mock_count = MagicMock(return_value=0)
    args = argparse.Namespace(enabled=False, lease_command=command)
    with patch(
        "maestro.gate_capacity.measure_host_capacity",
        return_value=gate_capacity.HostCapacity(cores=slots, available_mib=8000),
    ), patch(
        "maestro.gate_capacity.count_in_flight", mock_count,
    ), patch(
        "maestro.gate_capacity.plan_lease",
        return_value=gate_capacity.GateLease(workers=workers, slots=slots),
    ), patch(
        "maestro.gate_capacity.acquire_slot", side_effect=fake_acquire,
    ), patch(
        # This helper drives the real seam but asserts only the wrapper's
        # slot/argv behaviour; stub the telemetry writer so it does no real
        # I/O (the recording path has its own mem_fs-backed test below).
        "maestro.gate_capacity.record_gate_run",
    ), patch(
        "maestro.cli.subprocess.Popen", side_effect=fake_run,
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, config)
    code = exc.value.code
    assert isinstance(code, int)
    # r9: the slot seams get the canonical lock dir and the planned slot budget.
    assert acquire_calls == [(expected_slot_dir, slots)]
    if admission == "slot":
        mock_count.assert_called_once_with(expected_slot_dir, slots)
    else:
        # Fail-open takes the one-worker floor without reading concurrency.
        mock_count.assert_not_called()
    return code, events, run_argv[0]


def test_gate_lease_wrapper_slot_admission_substitutes_granted_workers() -> None:
    """On a 'slot' admission, {worker_args} splices the granted width's atoms."""
    code, events, argv = _run_lease_wrapper(
        ["pytest", "{worker_args}"], admission="slot", returncode=0, workers=4,
    )
    assert code == 0
    assert argv == ["pytest", "-n", "4", "--dist", "loadfile"]
    # The child runs *inside* the slot: acquire, then run, then release.
    assert events == ["acquire", "run", "release"]


def test_gate_lease_wrapper_single_worker_grant_runs_serial() -> None:
    """A granted single worker collapses to serial: {worker_args} splices no atoms.

    ``gate_capacity.worker_args`` returns ``()`` for a ≤1-worker grant — a single
    worker buys no parallelism yet pays for a worker process — so the wrapper drops
    the ``{worker_args}`` token entirely and the child runs a plain serial suite
    (no ``-n``, no ``--dist``), matching ``worker_args(lease)``. This is the case
    the shell/doc lineage previously ran as ``-n 1 --dist loadfile``.
    """
    code, events, argv = _run_lease_wrapper(
        ["pytest", "{worker_args}"], admission="slot", returncode=0, workers=1,
    )
    assert code == 0
    assert argv == ["pytest"]
    assert events == ["acquire", "run", "release"]


def test_gate_lease_wrapper_fallback_admission_runs_serial() -> None:
    """On a 'fallback' admission, the one-worker floor splices no atoms — serial.

    The fail-open floor under saturation is a one-worker grant, so
    ``worker_args`` returns ``()`` and ``{worker_args}`` is dropped: a saturated
    host runs the child serially (no ``-n``) rather than under ``-n 1``.
    """
    code, events, argv = _run_lease_wrapper(
        ["pytest", "{worker_args}"], admission="fallback", returncode=0, workers=4,
    )
    assert code == 0
    assert argv == ["pytest"]
    assert events == ["acquire", "run", "release"]


def test_gate_lease_wrapper_releases_slot_on_nonzero_exit() -> None:
    """A non-zero child exit still releases the slot and propagates the code."""
    code, events, _argv = _run_lease_wrapper(
        ["pytest", "{worker_args}"], admission="slot", returncode=2,
    )
    assert code == 2
    assert events == ["acquire", "run", "release"]


def test_gate_lease_wrapper_signal_death_maps_to_shell_convention() -> None:
    """A child killed by signal N (returncode -N) releases the slot and exits 128+N.

    ``sys.exit(-15)`` would surface as ``256 - 15 == 241`` at the shell, not the
    conventional ``143``; _run_gate_child maps the negative code to ``128 + N``
    so the documented "exits with the child's return code" holds (r7).
    """
    code, events, _argv = _run_lease_wrapper(
        ["pytest", "{worker_args}"], admission="slot", returncode=-15,
    )
    assert code == 143  # 128 + 15 (SIGTERM)
    assert events == ["acquire", "run", "release"]


def test_gate_lease_wrapper_flag_off_takes_no_slot_and_substitutes_auto() -> None:
    """Flag off: {worker_args} splices '-n auto --dist loadfile', no slot/host read."""
    run_argv: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        run_argv.append(argv)
        return _fake_child(0)

    args = argparse.Namespace(enabled=False, lease_command=["pytest", "{worker_args}"])
    with patch("maestro.gate_capacity.acquire_slot") as mock_acq, \
            patch("maestro.gate_capacity.measure_host_capacity") as mock_measure, \
            patch("maestro.cli.subprocess.Popen", side_effect=fake_run), \
            pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, _make_config(gate_lease_enabled=False))
    assert exc.value.code == 0
    assert run_argv[0] == ["pytest", "-n", "auto", "--dist", "loadfile"]
    mock_acq.assert_not_called()
    mock_measure.assert_not_called()


def test_gate_lease_wrapper_strips_leading_double_dash() -> None:
    """A leading '--' left by argparse.REMAINDER is dropped from the child argv."""
    run_argv: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        run_argv.append(argv)
        return _fake_child(0)

    args = argparse.Namespace(enabled=False, lease_command=["--", "echo", "ok"])
    with patch("maestro.cli.subprocess.Popen", side_effect=fake_run), \
            pytest.raises(SystemExit):
        cmd_gate_lease(args, _make_config(gate_lease_enabled=False))
    assert run_argv[0] == ["echo", "ok"]


def test_gate_lease_wrapper_empty_command_is_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No command after -- is a usage error: exit 2, message on stderr, no child run."""
    args = argparse.Namespace(enabled=False, lease_command=[])
    with patch("maestro.cli.subprocess.Popen") as mock_run, \
            pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, _make_config(gate_lease_enabled=False))
    assert exc.value.code == 2
    mock_run.assert_not_called()
    assert "no command given" in capsys.readouterr().err


def test_gate_lease_wrapper_flag_on_empty_command_skips_host_read() -> None:
    """r8: an empty child is rejected before any host read or slot wait, flag on.

    Otherwise `maestro gate-lease` with nothing after -- would measure the host
    and burn the bounded acquire wait under saturation before the usage error.
    """
    args = argparse.Namespace(enabled=False, lease_command=[])
    with patch("maestro.gate_capacity.measure_host_capacity") as mock_measure, \
            patch("maestro.gate_capacity.acquire_slot") as mock_acq, \
            pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, _make_config(gate_lease_enabled=True))
    assert exc.value.code == 2
    mock_measure.assert_not_called()
    mock_acq.assert_not_called()


def test_gate_lease_wrapper_missing_binary_exits_127(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """r7: a child binary not on PATH exits 127 (not a raw FileNotFoundError)."""
    args = argparse.Namespace(
        enabled=False, lease_command=["definitely-not-a-real-binary", "{worker_args}"],
    )
    with patch(
        "maestro.cli.subprocess.Popen", side_effect=FileNotFoundError("no such file"),
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, _make_config(gate_lease_enabled=False))
    assert exc.value.code == 127
    assert "command not found" in capsys.readouterr().err


def test_gate_lease_registered_in_handlers() -> None:
    """The gate-lease subcommand is wired into the parser and handler map."""
    with patch.object(sys, "argv", ["maestro", "gate-lease", "--enabled"]), \
            patch("maestro.cli.cmd_gate_lease") as mock_handler, \
            patch("maestro.cli.Config.from_env", return_value=_make_config()):
        from maestro.cli import main
        main()
    mock_handler.assert_called_once()


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, "CLEAN"),
        (127, "UNAVAILABLE"),  # missing binary — could not be executed
        (143, "TIMEOUT"),      # 128 + 15, signal death — a killed runner
        (137, "TIMEOUT"),      # 128 + 9 (SIGKILL, e.g. OOM)
        (1, "UNKNOWN"),        # an ordinary red the wrapper does not classify
        (2, "UNKNOWN"),
    ],
)
def test_gate_child_outcome_maps_child_status(returncode: int, expected: str) -> None:
    """The child-status -> outcome map mirrors the shell runner's classify_gate_run."""
    assert _gate_child_outcome(returncode) == expected


def test_gate_child_outcome_timeout_banner_maps_to_timeout() -> None:
    """A non-zero child that printed pytest-timeout's banner is ``TIMEOUT``.

    Exit ``0`` stays ``CLEAN`` whatever the output held, and an ordinary red
    without the banner stays ``UNKNOWN``.
    """
    assert _gate_child_outcome(1, saw_test_timeout=True) == "TIMEOUT"
    assert _gate_child_outcome(0, saw_test_timeout=True) == "CLEAN"
    assert _gate_child_outcome(1, saw_test_timeout=False) == "UNKNOWN"


def test_run_gate_child_forwards_streams_and_detects_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both streams are forwarded unchanged; a banner on either is detected."""
    banner = "+" * 20 + " Timeout " + "+" * 20 + "\n"
    child = _fake_child(1, stdout="collected 3 items\n", stderr=banner)
    with patch("maestro.cli.subprocess.Popen", return_value=child) as mock_popen:
        result = _run_gate_child(["pytest", "{worker_args}"], ("-n", "2"))

    assert result == (1, True)
    assert mock_popen.call_args.args[0] == ["pytest", "-n", "2"]
    out = capsys.readouterr()
    assert out.out == "collected 3 items\n"
    assert out.err == banner


def test_forward_gate_stream_keeps_draining_after_sink_error() -> None:
    """A closed sink stops the writes but not the drain or timeout detection."""
    sink = MagicMock()
    sink.write.side_effect = BrokenPipeError()
    banner = "+" * 20 + " Timeout " + "+" * 20 + "\n"
    source = io.StringIO("first\nsecond\n" + banner)
    saw_timeout = threading.Event()

    _forward_gate_stream(source, sink, saw_timeout)

    assert sink.write.call_count == 1
    assert source.read() == ""
    assert saw_timeout.is_set() is True


def test_run_gate_child_without_banner_reports_no_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordinary output is forwarded and does not read as a test timeout."""
    child = _fake_child(1, stdout="FAILED tests/t.py::test_x - assert 0\n")
    with patch("maestro.cli.subprocess.Popen", return_value=child):
        result = _run_gate_child(["pytest"], ())

    assert result == (1, False)
    assert capsys.readouterr().out == "FAILED tests/t.py::test_x - assert 0\n"


def test_gate_lease_wrapper_records_timeout_when_pytest_timeout_killed_it(
    mem_fs: dict[str, str],
) -> None:
    """A wrapper run pytest-timeout killed lands a ``TIMEOUT`` row, not ``UNKNOWN``."""
    config = _make_config(gate_lease_enabled=True)

    @contextmanager
    def fake_acquire(lock_dir: object, slots: int, wait_seconds: float = 0.0) -> Any:
        yield "slot"

    banner = "+" * 20 + " Timeout " + "+" * 20 + "\n"
    args = argparse.Namespace(enabled=False, lease_command=["pytest"])
    with patch(
        "maestro.gate_capacity.measure_host_capacity",
        return_value=gate_capacity.HostCapacity(cores=4, available_mib=8000),
    ), patch(
        "maestro.gate_capacity.count_in_flight", return_value=0,
    ), patch(
        "maestro.gate_capacity.acquire_slot", side_effect=fake_acquire,
    ), patch(
        "maestro.cli.subprocess.Popen", return_value=_fake_child(1, stderr=banner),
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, config)

    assert exc.value.code == 1
    row = json.loads(mem_fs[str(config.gate_runs_path)].strip())
    assert row["outcome"] == "TIMEOUT"


def test_gate_lease_wrapper_records_one_row_with_child_outcome(
    mem_fs: dict[str, str],
) -> None:
    """Flag on, the wrapper records one telemetry row stamped with the child outcome.

    Exercises the real seam and the real recording path: a child exiting 0 lands
    exactly one row with outcome CLEAN under the derived gate_runs_path.
    """
    config = _make_config(gate_lease_enabled=True)

    @contextmanager
    def fake_acquire(lock_dir: object, slots: int, wait_seconds: float = 0.0) -> Any:
        yield "slot"

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        return _fake_child(0)

    args = argparse.Namespace(enabled=False, lease_command=["pytest", "{worker_args}"])
    with patch(
        "maestro.gate_capacity.measure_host_capacity",
        return_value=gate_capacity.HostCapacity(cores=4, available_mib=8000),
    ), patch(
        "maestro.gate_capacity.count_in_flight", return_value=0,
    ), patch(
        "maestro.gate_capacity.acquire_slot", side_effect=fake_acquire,
    ), patch(
        "maestro.cli.subprocess.Popen", side_effect=fake_run,
    ), pytest.raises(SystemExit) as exc:
        cmd_gate_lease(args, config)

    assert exc.value.code == 0
    lines = mem_fs[str(config.gate_runs_path)].strip().splitlines()
    assert len(lines) == 1
    record = gate_capacity._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "slot"
    assert record.outcome == "CLEAN"


def test_gate_check_threads_derived_slot_dir_from_config() -> None:
    """cmd_gate_check threads the derived config.gate_lease_dir into run_gate_check.

    Flag on, the slot dir is the configured gate_lease_slot_dir, so a loop-side
    gate-check joins the same host-scoped slot set as interactive runs (proposal 126).
    """
    from maestro.gate_check import GateCheckResult

    config = _make_config(gate_lease_enabled=True)
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result,
    ) as mock_check, pytest.raises(SystemExit) as exc:
        cmd_gate_check(argparse.Namespace(), config)

    assert exc.value.code == 0
    mock_check.assert_called_once_with(
        config.work_target(),
        lease_slot_dir=config.gate_lease_slot_dir,
        runs_path=config.gate_runs_path,
        cgroup_reserve=False,
        admission_cap=False,
        full=False,
    )


def test_gate_check_flag_off_threads_none_slot_dir() -> None:
    """Flag off, cmd_gate_check threads a None slot dir so run_gate_check takes no slot."""
    from maestro.gate_check import GateCheckResult

    config = _make_config(gate_lease_enabled=False)
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with patch(
        "maestro.gate_check.run_gate_check", return_value=result,
    ) as mock_check, pytest.raises(SystemExit):
        cmd_gate_check(argparse.Namespace(), config)

    mock_check.assert_called_once_with(
        config.work_target(),
        lease_slot_dir=None,
        runs_path=config.gate_runs_path,
        cgroup_reserve=False,
        admission_cap=False,
        full=False,
    )


def test_gate_check_prints_and_persists_the_runnability_verdict(
    mem_fs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Flag on, gate-check probes first, prints on stderr and writes the row.

    The line goes to *stderr* beside the gate diagnostics so stdout stays the
    single parseable ``--json`` line, and the refreshed row lands where the
    loop's own consumers read it next — so a repair the operator just proved
    releases the lease seam's refusals without waiting for the loop's probe.

    The sibling finder is bound to the fully-resolved stand-in: the provisioning
    probe runs first in the oracle's precedence, so an unpatched test would read
    *this interpreter's* installs and go ``UNPROVISIONED`` on any checkout
    lacking both editable siblings — a real-environment dependency the unit
    suite must not carry.
    """
    from maestro.gate_check import GateCheckResult

    _sibling_finder(monkeypatch, dict(_SIBLINGS_RESOLVED))
    config = _make_config(gate_environment_oracle_enabled=True)
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    collected = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="3 tests collected", stderr="",
    )
    with (
        patch("maestro.gate_check.run_gate_check", return_value=result),
        patch("maestro.gate_check.subprocess.run", return_value=collected),
        patch("maestro.gate_check._origin_main_head", return_value="head"),
        pytest.raises(SystemExit) as exc,
    ):
        cmd_gate_check(argparse.Namespace(json=True), config)

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "gate runnability: SOUND" in captured.err
    payload = json.loads(captured.out)
    assert payload["runnability"] == "SOUND"
    assert json.loads(mem_fs[str(config.gate_environment_path)])["kind"] == "SOUND"


@pytest.mark.parametrize("budget_lease_owned", [True, False])
def test_gate_check_forwards_the_budget_lease_flag(
    mem_fs: dict[str, str], budget_lease_owned: bool,
) -> None:
    """The handler hands the refresher ``gate_budget_lease_owned_enabled``.

    The fit reading must be judged against the budget the gate run actually
    spends, and only the config knows which one that is (proposal 134).
    """
    from maestro.gate_check import GateCheckResult

    config = _make_config(
        gate_environment_oracle_enabled=True,
        gate_budget_lease_owned_enabled=budget_lease_owned,
    )
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with (
        patch("maestro.gate_check.run_gate_check", return_value=result),
        patch(
            "maestro.gate_check.refresh_runnability_verdict",
            return_value=gate_environment.GateRunnabilityVerdict(
                kind="SOUND", fingerprint="f",
            ),
        ) as mock_refresh,
        pytest.raises(SystemExit),
    ):
        cmd_gate_check(argparse.Namespace(json=False), config)

    assert mock_refresh.call_args.kwargs["budget_lease_owned"] is budget_lease_owned


@pytest.mark.parametrize("cgroup_reserve", [True, False])
def test_gate_check_forwards_the_cgroup_reserve_flag(
    mem_fs: dict[str, str], cgroup_reserve: bool,
) -> None:
    """The refresher plans the oracle width under ``gate_lease_cgroup_reserve_enabled``,
    the same setting the gate run it then performs is admitted under."""
    from maestro.gate_check import GateCheckResult

    config = _make_config(
        gate_environment_oracle_enabled=True,
        gate_lease_cgroup_reserve_enabled=cgroup_reserve,
    )
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with (
        patch("maestro.gate_check.run_gate_check", return_value=result),
        patch(
            "maestro.gate_check.refresh_runnability_verdict",
            return_value=gate_environment.GateRunnabilityVerdict(
                kind="SOUND", fingerprint="f",
            ),
        ) as mock_refresh,
        pytest.raises(SystemExit),
    ):
        cmd_gate_check(argparse.Namespace(json=False), config)

    assert mock_refresh.call_args.kwargs["cgroup_reserve"] is cgroup_reserve


def test_gate_check_flag_off_issues_no_probe_and_changes_no_output(
    mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    """Flag off, the command is byte-identical to today: no probe, line or row."""
    from maestro.gate_check import GateCheckResult

    config = _make_config(gate_environment_oracle_enabled=False)
    result = GateCheckResult({"ruff": 0, "mypy": 0, "pytest": 0})
    with (
        patch("maestro.gate_check.run_gate_check", return_value=result),
        patch("maestro.gate_check.refresh_runnability_verdict") as mock_refresh,
        pytest.raises(SystemExit),
    ):
        cmd_gate_check(argparse.Namespace(json=True), config)

    mock_refresh.assert_not_called()
    captured = capsys.readouterr()
    assert "gate runnability" not in captured.err
    assert "runnability" not in json.loads(captured.out)
    assert str(config.gate_environment_path) not in mem_fs


# ---------------------------------------------------------------------------
# cmd_gate_scope (proposal 134)
# ---------------------------------------------------------------------------


def _gate_scope_args(**overrides: object) -> argparse.Namespace:
    """Build the gate-scope namespace by hand, mirroring argparse's defaults."""
    fields: dict[str, object] = {"path": None, "plan": None, "json": False}
    fields.update(overrides)
    return argparse.Namespace(**fields)


def test_gate_scope_subcommand_registered(mem_fs: dict[str, str]) -> None:
    """main() routes 'gate-scope' to its handler.

    Reaching the handler proves argparse registered the subcommand and the
    dispatch table agrees with the parser — an unregistered name exits with
    argparse's code-2 'invalid choice'.
    """
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "gate-scope"]), \
         patch("maestro.cli.cmd_gate_scope") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_gate_scope_empty_selection_human(mem_fs: dict[str, str]) -> None:
    """A docs-only diff prints the 'full' sentinel line rather than a table."""
    config = _make_config()
    args = _gate_scope_args(path=["README.md"])
    with patch.object(Path, "rglob", return_value=[]):
        output = capture_stdout(cmd_gate_scope, args, config)
    assert "Gate Scope" in output
    assert "Changed paths: 1" in output
    assert "Selection: none — scope 'full' (do not narrow)." in output


def test_cmd_gate_scope_empty_selection_json(mem_fs: dict[str, str]) -> None:
    """--json on an empty selection emits the documented key set, scope 'full'."""
    config = _make_config()
    args = _gate_scope_args(path=["README.md"], json=True)
    with patch.object(Path, "rglob", return_value=[]):
        output = capture_stdout(cmd_gate_scope, args, config)
    assert json.loads(output) == {
        "changed_paths": ["README.md"],
        "selected": [],
        "selected_count": 0,
        "total_test_modules": 0,
        "selected_fraction": 0.0,
        "scope": "full",
    }


def test_cmd_gate_scope_populated_human_output(mem_fs: dict[str, str]) -> None:
    """A narrowing diff lists each selected module and the selected fraction."""
    config = _make_config()
    paired = FAKE_DIR / "tests" / "test_memory.py"
    other = FAKE_DIR / "tests" / "test_journal.py"
    mem_fs[str(paired)] = "x = 1\n"
    mem_fs[str(other)] = "from maestro import journal\n"
    args = _gate_scope_args(path=["maestro/memory.py"])
    with patch.object(Path, "rglob", return_value=[paired, other]):
        output = capture_stdout(cmd_gate_scope, args, config)
    assert "tests/test_memory.py" in output
    assert "tests/test_journal.py" not in output
    assert "Selected: 1 of 2 test modules (50.0%)" in output
    assert "Scope: impacted" in output


def test_cmd_gate_scope_populated_json(mem_fs: dict[str, str]) -> None:
    """--json round-trips the selection and its measured fraction."""
    config = _make_config()
    paired = FAKE_DIR / "tests" / "test_memory.py"
    other = FAKE_DIR / "tests" / "test_journal.py"
    mem_fs[str(paired)] = "x = 1\n"
    mem_fs[str(other)] = "from maestro import journal\n"
    args = _gate_scope_args(path=["maestro/memory.py"], json=True)
    with patch.object(Path, "rglob", return_value=[paired, other]):
        output = capture_stdout(cmd_gate_scope, args, config)
    assert json.loads(output) == {
        "changed_paths": ["maestro/memory.py"],
        "selected": ["tests/test_memory.py"],
        "selected_count": 1,
        "total_test_modules": 2,
        "selected_fraction": 0.5,
        "scope": "impacted",
    }


def test_cmd_gate_scope_fraction_excludes_non_test_modules(
    mem_fs: dict[str, str],
) -> None:
    """The denominator counts collectable test modules only.

    Regression: ``total`` used to be every ``.py`` under the tests tree, so a
    conftest and a helper inflated it and systematically understated the
    headline fraction this command exists to produce.
    """
    config = _make_config()
    paired = FAKE_DIR / "tests" / "test_memory.py"
    other = FAKE_DIR / "tests" / "test_journal.py"
    conftest = FAKE_DIR / "tests" / "conftest.py"
    helper = FAKE_DIR / "tests" / "support" / "fakes.py"
    mem_fs[str(paired)] = "x = 1\n"
    mem_fs[str(other)] = "from maestro import journal\n"
    mem_fs[str(conftest)] = "import maestro.memory\n"
    mem_fs[str(helper)] = "import maestro.memory\n"
    args = _gate_scope_args(path=["maestro/memory.py"], json=True)
    with patch.object(
        Path, "rglob", return_value=[paired, other, conftest, helper],
    ):
        output = capture_stdout(cmd_gate_scope, args, config)
    data = json.loads(output)
    assert data["selected"] == ["tests/test_memory.py"]
    assert data["total_test_modules"] == 2
    assert data["selected_fraction"] == 0.5


def test_cmd_gate_scope_reads_plan_files_affected(mem_fs: dict[str, str]) -> None:
    """--plan resolves the changed paths through Memory.get_plan."""
    config = _make_config()
    paired = FAKE_DIR / "tests" / "test_memory.py"
    mem_fs[str(paired)] = "x = 1\n"
    plan = MagicMock()
    plan.files_affected = ["maestro/memory.py"]
    args = _gate_scope_args(plan="plan-1", json=True)
    with patch.object(Path, "rglob", return_value=[paired]), \
         patch("maestro.memory.Memory.get_plan", return_value=plan):
        output = capture_stdout(cmd_gate_scope, args, config)
    data = json.loads(output)
    assert data["changed_paths"] == ["maestro/memory.py"]
    assert data["selected"] == ["tests/test_memory.py"]


def test_cmd_gate_scope_missing_plan_reports_and_returns(
    mem_fs: dict[str, str],
) -> None:
    """An unknown plan id prints the not-found line and reads no tests tree."""
    config = _make_config()
    args = _gate_scope_args(plan="nope")
    with patch("maestro.memory.Memory.get_plan", return_value=None), \
         patch.object(Path, "rglob") as mock_rglob:
        output = capture_stdout(cmd_gate_scope, args, config)
    assert "Plan not found: nope" in output
    mock_rglob.assert_not_called()


def _seed_planning_grounding_plans(config: Config) -> None:
    """Two rejections (one tree-checkable) and a merged plan with reads."""
    memory = Memory(config.memory_dir)
    old = _doa_cli_plan("old", status="rejected",
                        created_at="2026-08-01T00:00:00Z")
    old.rejection_reason = "already implemented"
    memory.save_evolution_plan(old)
    new = _doa_cli_plan("new", status="rejected",
                        created_at="2026-09-01T00:00:00Z")
    new.rejection_reason = "too broad"
    memory.save_evolution_plan(new)


def test_measure_planning_grounding_subcommand_registered(
    mem_fs: dict[str, str],
) -> None:
    """main() routes 'measure-planning-grounding' to its handler."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-planning-grounding"]), \
         patch("maestro.cli.cmd_measure_planning_grounding") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_measure_planning_grounding_rejects_bad_since(
    mem_fs: dict[str, str],
) -> None:
    """A malformed --since is an argparse usage error (exit 2)."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-planning-grounding",
                            "--since", "not-a-date"]), \
         patch("maestro.cli.cmd_measure_planning_grounding") as mock_cmd, \
         pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    mock_cmd.assert_not_called()


def test_measure_planning_grounding_parses_since(mem_fs: dict[str, str]) -> None:
    """A valid --since reaches the handler as an ISO date string."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "measure-planning-grounding",
                            "--since", "2026-08-16"]), \
         patch("maestro.cli.cmd_measure_planning_grounding") as mock_cmd:
        main()

    args = mock_cmd.call_args.args[0]
    assert args.since == "2026-08-16"


def test_cmd_measure_planning_grounding_empty(mem_fs: dict[str, str]) -> None:
    """No plans prints zero counts and 'no reads recorded' coverage."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(since=None, json=False)
    output = capture_stdout(cmd_measure_planning_grounding, args, config)
    assert "Decided plans:         0" in output
    assert "Read coverage:         no reads recorded" in output
    assert "Since:" not in output


def test_cmd_measure_planning_grounding_human_output(
    mem_fs: dict[str, str],
) -> None:
    """Seeded rejections print the tree-checkable share."""
    config = _make_config()
    config.ensure_dirs()
    _seed_planning_grounding_plans(config)
    args = argparse.Namespace(since=None, json=False)
    output = capture_stdout(cmd_measure_planning_grounding, args, config)
    assert "Rejected:              2" in output
    assert "Tree-checkable:        1 (50.0% of rejected)" in output
    assert "No reads recorded:     2" in output


def test_cmd_measure_planning_grounding_with_coverage(
    mem_fs: dict[str, str],
) -> None:
    """A plan with grounded_reads prints a coverage percentage."""
    config = _make_config()
    config.ensure_dirs()
    plan = _doa_cli_plan("read", status="merged")
    plan.grounded_reads = ["maestro/x.py"]
    Memory(config.memory_dir).save_evolution_plan(plan)
    args = argparse.Namespace(since=None, json=False)
    output = capture_stdout(cmd_measure_planning_grounding, args, config)
    assert "Read coverage:         100.0%" in output


def test_cmd_measure_planning_grounding_refusal_split(
    mem_fs: dict[str, str],
) -> None:
    """Non-zero refusal tags print indented; --json carries refusals_by_tag."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    read = _doa_cli_plan("read", status="obsolete")
    read.obsolete_reason = "[ungrounded:duplicate] probe [planner-read]"
    memory.save_evolution_plan(read)
    cli = _doa_cli_plan("cli", status="obsolete")
    cli.obsolete_reason = "Cites unshipped CLI evidence: maestro x"
    memory.save_evolution_plan(cli)
    args = argparse.Namespace(since=None, json=False)
    output = capture_stdout(cmd_measure_planning_grounding, args, config)
    assert "  ungrounded:read:     1" in output
    assert "  cli-evidence:        1" in output
    assert "Other grounding refusals:" in output
    assert output.index("Other grounding refusals:") < output.index("cli-evidence")
    assert "ungrounded:unread" not in output
    args = argparse.Namespace(since=None, json=True)
    report = json.loads(capture_stdout(cmd_measure_planning_grounding, args, config))
    assert report["refusals_by_tag"]["ungrounded:read"] == 1
    assert report["door_refusals"] == 1


def test_cmd_measure_planning_grounding_no_other_header(
    mem_fs: dict[str, str],
) -> None:
    """Only ungrounded:* refusals -> no 'Other grounding refusals' header."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    read = _doa_cli_plan("read", status="obsolete")
    read.obsolete_reason = "[ungrounded:duplicate] probe [planner-unread]"
    memory.save_evolution_plan(read)
    args = argparse.Namespace(since=None, json=False)
    output = capture_stdout(cmd_measure_planning_grounding, args, config)
    assert "  ungrounded:unread:   1" in output
    assert "Other grounding refusals:" not in output


def test_cmd_measure_planning_grounding_json_since(
    mem_fs: dict[str, str],
) -> None:
    """--json with --since keeps only the newer rejection."""
    config = _make_config()
    config.ensure_dirs()
    _seed_planning_grounding_plans(config)
    args = argparse.Namespace(since="2026-08-16", json=True)
    output = capture_stdout(cmd_measure_planning_grounding, args, config)
    data = json.loads(output)
    assert data["rejected"] == 1
    assert data["tree_checkable"] == 0
    assert data["read_coverage"] is None


def test_cmd_measure_planning_grounding_since_human(
    mem_fs: dict[str, str],
) -> None:
    """The human table echoes the --since bound."""
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(since="2026-08-16", json=False)
    output = capture_stdout(cmd_measure_planning_grounding, args, config)
    assert "Since:                 2026-08-16" in output


def test_cmd_measure_planning_grounding_read_only(
    mem_fs: dict[str, str],
) -> None:
    """The command writes nothing."""
    config = _make_config()
    config.ensure_dirs()
    _seed_planning_grounding_plans(config)
    before = dict(mem_fs)
    args = argparse.Namespace(since=None, json=True)
    capture_stdout(cmd_measure_planning_grounding, args, config)
    assert mem_fs == before
