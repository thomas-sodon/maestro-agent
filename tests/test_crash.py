"""Tests for :mod:`maestro.crash` — crash detection and reporting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.crash import (
    _MAX_RENDERED_STDERR_CHARS,
    CrashEvent,
    build_crash_event,
    handle_crash,
)
from maestro.diagnostics import DiagnosticSnapshot
from maestro.sdk import ErrorCategory, SDKError

# ── _read_lifecycle_progress crash detection ──────────────────────────


def test_detect_crash_from_lifecycle(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps(
        {"iteration": 5, "next_phase": "contemplate"}
    )
    last_shutdown, it, phase, last_exit_code, crashed, *_ = agent._read_lifecycle_progress()
    assert last_shutdown is None
    assert it == 5
    assert phase == "contemplate"
    assert last_exit_code is None
    assert crashed is True


def test_graceful_shutdown_not_crash(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps(
        {
            "last_shutdown": "2026-04-05T10:00:00+00:00",
            "iteration": 5,
            "next_phase": "dream",
        }
    )
    _last, _it, _ph, _ec, crashed, *_ = agent._read_lifecycle_progress()
    assert crashed is False


def test_missing_lifecycle_not_crash(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    # Do not seed lifecycle.json
    last, it, ph, last_exit_code, crashed, *_ = agent._read_lifecycle_progress()
    assert (last, it, ph, last_exit_code, crashed) == (None, 0, None, None, False)


def test_zero_iteration_no_next_phase_not_crash(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """A file with iteration=0 and no next_phase isn't a crash signal."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps(
        {"iteration": 0, "next_phase": None}
    )
    _, _, _, _, crashed, *_ = agent._read_lifecycle_progress()
    assert crashed is False


def test_corrupt_lifecycle_not_crash(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = "{not json"
    assert agent._read_lifecycle_progress() == (
        None, 0, None, None, False, "", "", None, None, None, None,
        False, None,
        0, 0, 0, 0,
        False,
    )


# ── build_crash_event ─────────────────────────────────────────────────


def test_build_crash_event_gathers_context(mem_fs: dict[str, str]) -> None:
    log_path = Path("/fake/logs/maestro.log")
    mem_fs[str(log_path)] = (
        "2026-04-05 10:00:00,000 [INFO] maestro: Started\n"
        "2026-04-05 10:00:01,000 [ERROR] maestro: Crash here\n"
    )
    event = build_crash_event(
        last_iteration=3, last_phase="contemplate", log_path=log_path
    )
    assert event.last_iteration == 3
    assert event.last_phase == "contemplate"
    assert event.detected_at  # ISO timestamp
    assert len(event.log_tail) == 2
    assert event.diagnostic is not None


def test_build_crash_event_no_log_path() -> None:
    event = build_crash_event(last_iteration=1, last_phase=None, log_path=None)
    assert event.log_tail == []


def test_build_crash_event_accepts_exit_code() -> None:
    event = build_crash_event(
        last_iteration=2,
        last_phase="dream",
        log_path=None,
        exit_code=1,
    )
    assert event.exit_code == 1


def test_build_crash_event_defaults_exit_code_none() -> None:
    event = build_crash_event(
        last_iteration=2, last_phase="dream", log_path=None,
    )
    assert event.exit_code is None


def test_build_crash_event_carries_sdk_context() -> None:
    """The reconstituted SDKError reaches ``CrashEvent.sdk_error`` whole."""
    err = SDKError(
        "process died",
        category=ErrorCategory.RATE_LIMIT,
        exit_code=143,
        stderr="rate limit exceeded",
        retries_attempted=3,
    )
    event = build_crash_event(
        last_iteration=7,
        last_phase="act",
        log_path=None,
        exit_code=143,
        sdk_error=err,
    )
    assert event.sdk_error is err
    assert event.sdk_error.category is ErrorCategory.RATE_LIMIT
    assert event.sdk_error.exit_code == 143
    assert event.sdk_error.stderr == "rate limit exceeded"
    assert event.sdk_error.retries_attempted == 3


def test_build_crash_event_defaults_sdk_context_none() -> None:
    event = build_crash_event(
        last_iteration=2, last_phase="dream", log_path=None,
    )
    assert event.sdk_error is None


# ── handle_crash ──────────────────────────────────────────────────────


def _make_handle_crash_args(mem_fs: dict[str, str]) -> dict[str, Any]:
    """Build mocks for handle_crash; returns a dict of kwargs to inject."""
    d = Path("/fake/crash")
    config = Config(memory_dir=d, workspace=d, github_repo="owner/repo")
    config.ensure_dirs()

    from maestro.journal import Journal

    journal = Journal(d)

    notifier = MagicMock()
    notifier.notify = AsyncMock()

    return {
        "notifier": notifier,
        "journal": journal,
    }


def _make_event() -> CrashEvent:
    return CrashEvent(
        last_iteration=4,
        last_phase="evolve",
        detected_at="2026-04-05T10:00:00+00:00",
        log_tail=[
            {
                "timestamp": "2026-04-05T10:00:00+00:00",
                "level": "ERROR",
                "logger": "maestro",
                "message": "Unhandled exception",
            }
        ],
        diagnostic=DiagnosticSnapshot(
            recent_error_count=1,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=1,
            summary="1 recent error(s)",
        ),
    )


@pytest.mark.asyncio
async def test_handle_crash_pings_telegram(mem_fs: dict[str, str]) -> None:
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    await handle_crash(event, **kwargs)

    kwargs["notifier"].notify.assert_awaited_once()
    ping = kwargs["notifier"].notify.call_args[0][0]
    assert "phase=evolve" in ping
    assert "iter=4" in ping
    assert "Check app logs" in ping


@pytest.mark.asyncio
async def test_handle_crash_journals_milestone(
    mem_fs: dict[str, str],
) -> None:
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    await handle_crash(event, **kwargs)

    journal_path = Path("/fake/crash") / "events.jsonl"
    contents = mem_fs[str(journal_path)]
    entry = json.loads(contents.strip().splitlines()[0])
    assert entry["entry_type"] == "milestone"
    assert entry["triggered_by"] == "crash"
    assert "evolve" in entry["content"]


def _read_crash_milestone(mem_fs: dict[str, str]) -> str:
    journal_path = Path("/fake/crash") / "events.jsonl"
    entry = json.loads(mem_fs[str(journal_path)].strip().splitlines()[0])
    content = entry["content"]
    assert isinstance(content, str)
    return content


@pytest.mark.asyncio
async def test_handle_crash_surfaces_sdk_context(
    mem_fs: dict[str, str],
) -> None:
    """Category, exit code, retries, and stderr head reach both surfaces."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = SDKError(
        "process died",
        category=ErrorCategory.RATE_LIMIT,
        exit_code=143,
        stderr="rate limit exceeded",
        retries_attempted=3,
    )
    await handle_crash(event, **kwargs)

    ping = kwargs["notifier"].notify.call_args[0][0]
    assert "category=rate_limit" in ping
    assert "exit=143" in ping
    assert "retries=3" in ping
    # The log-tail framing is augmented, never replaced.
    assert "phase=evolve" in ping
    assert "Check app logs" in ping

    content = _read_crash_milestone(mem_fs)
    assert "category=rate_limit" in content
    assert "exit=143" in content
    assert "retries=3" in content
    assert "stderr: rate limit exceeded" in content
    assert "1 recent error(s)" in content


@pytest.mark.asyncio
async def test_handle_crash_without_sdk_context_renders_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """No SDK context and no exit code → exactly the pre-existing text."""
    kwargs = _make_handle_crash_args(mem_fs)
    await handle_crash(_make_event(), **kwargs)

    ping = kwargs["notifier"].notify.call_args[0][0]
    assert ping == (
        "Maestro crashed (phase=evolve, iter=4). Check app logs for details."
    )
    content = _read_crash_milestone(mem_fs)
    assert content == (
        "I crashed. Last known phase: evolve at iteration 4. "
        "Diagnostic: 1 recent error(s)."
    )


@pytest.mark.asyncio
async def test_handle_crash_names_exit_code_without_sdk_context(
    mem_fs: dict[str, str],
) -> None:
    """``exit_code`` alone still gets a reader on both surfaces."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.exit_code = 9
    await handle_crash(event, **kwargs)

    assert "exit=9" in kwargs["notifier"].notify.call_args[0][0]
    content = _read_crash_milestone(mem_fs)
    # A non-SDK death is labelled neutrally — never narrated as SDK context.
    assert "Exit code: 9." in content
    assert "SDK context" not in content
    assert "stderr" not in content


@pytest.mark.asyncio
async def test_handle_crash_sdk_context_falls_back_to_event_exit_code(
    mem_fs: dict[str, str],
) -> None:
    """An SDKError with no exit code borrows the event's, and bounds stderr."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.exit_code = 4
    event.sdk_error = SDKError(
        "died",
        category=ErrorCategory.UNKNOWN,
        stderr="x" * 900,
    )
    await handle_crash(event, **kwargs)

    assert "exit=4" in kwargs["notifier"].notify.call_args[0][0]
    content = _read_crash_milestone(mem_fs)
    assert "x" * _MAX_RENDERED_STDERR_CHARS in content
    assert "x" * (_MAX_RENDERED_STDERR_CHARS + 1) not in content


@pytest.mark.asyncio
async def test_handle_crash_stderr_head_is_credential_scrubbed(
    mem_fs: dict[str, str],
) -> None:
    """Tokens in stderr never reach the journal, which feeds prompts + API."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = SDKError(
        "died",
        category=ErrorCategory.CLI_CRASH,
        exit_code=1,
        stderr=(
            "auth failed sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA "
            "Authorization: Bearer abcdefghijklmnop"
        ),
    )
    await handle_crash(event, **kwargs)

    content = _read_crash_milestone(mem_fs)
    assert "sk-ant-" not in content
    assert "abcdefghijklmnop" not in content
    assert "<redacted>" in content
    # The non-credential narrative around the token survives.
    assert "auth failed" in content


@pytest.mark.asyncio
async def test_handle_crash_stderr_head_is_whitespace_collapsed(
    mem_fs: dict[str, str],
) -> None:
    """A multi-line traceback collapses to one line and ends the sentence."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = SDKError(
        "died",
        category=ErrorCategory.CLI_CRASH,
        exit_code=1,
        stderr="Traceback:\n  File 'x.py'\n\tboom\n",
    )
    await handle_crash(event, **kwargs)

    content = _read_crash_milestone(mem_fs)
    assert "\n" not in content
    assert "\t" not in content
    assert "stderr: Traceback: File 'x.py' boom." in content


# ── #4591: rendered stderr is the debug-filtered tail, not the head ──

_DEBUG_NOISE_LINE = "[DEBUG] Watching for changes in setting files ...\n"
_ERROR_LINE = "Error: connect ECONNREFUSED 127.0.0.1:443\n"


def _rendered_stderr_clause(content: str) -> str:
    """The text between ``stderr: `` and the milestone's closing period."""
    assert "stderr: " in content
    clause = content.split("stderr: ", 1)[1]
    end = clause.rindex(".")
    return clause[:end]


@pytest.mark.asyncio
async def test_handle_crash_stderr_keeps_error_line_after_debug_noise(
    mem_fs: dict[str, str],
) -> None:
    """A live debug-first stream renders its trailing error line, no noise."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = SDKError(
        "died",
        category=ErrorCategory.NETWORK,
        exit_code=1,
        stderr=_DEBUG_NOISE_LINE * 40 + _ERROR_LINE,
    )
    await handle_crash(event, **kwargs)

    content = _read_crash_milestone(mem_fs)
    assert "stderr: Error: connect ECONNREFUSED 127.0.0.1:443." in content
    assert "[DEBUG]" not in content


@pytest.mark.asyncio
async def test_handle_crash_all_debug_stderr_renders_tail(
    mem_fs: dict[str, str],
) -> None:
    """An all-debug stream renders its tail within the cap."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    lines = [f"[DEBUG] n={i}" for i in range(200)]
    stderr = "\n".join(lines)
    assert len(stderr) > _MAX_RENDERED_STDERR_CHARS
    event.sdk_error = SDKError(
        "killed",
        category=ErrorCategory.CLI_CRASH,
        exit_code=137,
        stderr=stderr,
    )
    await handle_crash(event, **kwargs)

    clause = _rendered_stderr_clause(_read_crash_milestone(mem_fs))
    assert clause.endswith("n=199")
    assert "n=0 " not in clause
    assert len(clause) <= _MAX_RENDERED_STDERR_CHARS


@pytest.mark.asyncio
async def test_handle_crash_rebuilt_sdk_error_renders_error_line(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Persist → restart rebuild → milestone still names the error line."""
    agent = make_agent()
    agent._resume_iteration = 4
    agent._resume_phase = "evolve"
    agent._persist_crash_exit_code(
        None,
        sdk_error=SDKError(
            "died",
            category=ErrorCategory.NETWORK,
            exit_code=1,
            stderr=_DEBUG_NOISE_LINE * 40 + _ERROR_LINE,
        ),
    )
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = agent._read_crash_sdk_context()
    await handle_crash(event, **kwargs)

    content = _read_crash_milestone(mem_fs)
    assert "ECONNREFUSED" in content
    assert "[DEBUG]" not in content


def test_diagnostic_stderr_empty_string_returns_empty() -> None:
    from maestro import sdk

    assert sdk.diagnostic_stderr("") == ""


def test_diagnostic_stderr_drops_mixed_case_markers_keeping_case() -> None:
    from maestro import sdk

    text = "[debug] a\n[Debug] b\n[DEBUG] c\nError: Connect ECONNREFUSED\n"
    assert sdk.diagnostic_stderr(text) == "Error: Connect ECONNREFUSED"


def test_diagnostic_stderr_all_debug_returns_stream_unchanged() -> None:
    from maestro import sdk

    text = "[DEBUG] one\n[debug] two\n"
    assert sdk.diagnostic_stderr(text) == text


@pytest.mark.asyncio
async def test_handle_crash_blank_stderr_emits_no_label(
    mem_fs: dict[str, str],
) -> None:
    """Whitespace-only stderr collapses to nothing — no empty ``stderr:``."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = SDKError(
        "died",
        category=ErrorCategory.CLI_CRASH,
        exit_code=1,
        stderr="   \n\t ",
    )
    await handle_crash(event, **kwargs)

    content = _read_crash_milestone(mem_fs)
    assert "category=cli_crash" in content
    assert "stderr" not in content


@pytest.mark.asyncio
async def test_handle_crash_sdk_context_no_exit_code_anywhere(
    mem_fs: dict[str, str],
) -> None:
    """Neither the error nor the event knows the exit code → ``n/a``."""
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = SDKError("died", category=ErrorCategory.TIMEOUT)
    await handle_crash(event, **kwargs)

    assert "exit=n/a" in kwargs["notifier"].notify.call_args[0][0]
    content = _read_crash_milestone(mem_fs)
    assert "category=timeout" in content
    assert "stderr" not in content


@pytest.mark.asyncio
async def test_handle_crash_notifier_raises_swallowed(
    mem_fs: dict[str, str],
) -> None:
    kwargs = _make_handle_crash_args(mem_fs)
    kwargs["notifier"].notify = AsyncMock(side_effect=RuntimeError("telegram down"))
    event = _make_event()
    await handle_crash(event, **kwargs)  # must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal",
    [KeyboardInterrupt, SystemExit],
)
async def test_handle_crash_notifier_control_flow_propagates(
    mem_fs: dict[str, str],
    signal: type[BaseException],
) -> None:
    """A genuine ``KeyboardInterrupt``/``SystemExit`` from the notifier must
    propagate so an operator can abort a hung notifier (#465), not be swallowed."""
    kwargs = _make_handle_crash_args(mem_fs)
    kwargs["notifier"].notify = AsyncMock(side_effect=signal())
    event = _make_event()
    with pytest.raises(signal):
        await handle_crash(event, **kwargs)


@pytest.mark.asyncio
async def test_handle_crash_notifier_cancellederror_swallowed(
    mem_fs: dict[str, str],
) -> None:
    """A spurious ``asyncio.CancelledError`` from the notifier is swallowed.

    Unlike ``KeyboardInterrupt``/``SystemExit``, a residual SDK-teardown task
    can leak a ``CancelledError`` into the crash-ping Telegram POST; letting it
    escape would abort the startup path (the caller's guard is
    ``except Exception:``) and skip the crash journal milestone. Mirroring
    ``_notify_best_effort``, it is contained — ``handle_crash`` does not raise
    and the milestone below is still written."""
    kwargs = _make_handle_crash_args(mem_fs)
    kwargs["notifier"].notify = AsyncMock(side_effect=asyncio.CancelledError())
    event = _make_event()
    await handle_crash(event, **kwargs)  # must not raise

    # The journal milestone (the RCA artifact) is still written after the
    # contained notifier cancellation.
    journal_path = Path("/fake/crash") / "events.jsonl"
    entry = json.loads(mem_fs[str(journal_path)].strip().splitlines()[0])
    assert entry["entry_type"] == "milestone"
    assert entry["triggered_by"] == "crash"


@pytest.mark.asyncio
async def test_handle_crash_journal_write_failure_swallowed(
    mem_fs: dict[str, str],
) -> None:
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    with patch.object(
        kwargs["journal"], "record_event", side_effect=RuntimeError("bad")
    ):
        await handle_crash(event, **kwargs)  # must not raise


# ── run_loop integration ───────────────────────────────────────────────


def _stub_loop_phases(agent: Any) -> None:
    """Mock all phases so ``run_loop()`` can complete immediately."""
    agent._install_signal_handlers = MagicMock()
    agent._write_pid_file = MagicMock()
    agent.contributions.check_for_contributions = MagicMock()
    agent.contemplate = AsyncMock(return_value="reflection")
    agent._implement_approved_plans = AsyncMock()
    agent.evolve = AsyncMock()
    agent.generate_task = AsyncMock(return_value=(None, None))
    agent.run = AsyncMock()
    agent.shutdown = AsyncMock()


@pytest.mark.asyncio
async def test_run_loop_swallows_crash_reporting_exception(
    make_agent, mem_fs: dict[str, str]  # type: ignore[no-untyped-def]
) -> None:
    """If crash reporting itself raises, run_loop must log and continue."""
    agent = make_agent()
    _stub_loop_phases(agent)
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps(
        {"iteration": 1, "next_phase": "contemplate"}
    )
    with patch(
        "maestro.crash.build_crash_event",
        side_effect=RuntimeError("boom"),
    ):
        result = await agent.run_loop(
            max_iters=1,
            interval=0,
            task_every=0,
            evolve_every=0,
            dream_every=0,
        )
    # Loop continues after crash reporting failure
    assert result is True
    agent.contemplate.assert_awaited()


@pytest.mark.asyncio
async def test_run_loop_continues_after_crash_detection(
    make_agent, mem_fs: dict[str, str]  # type: ignore[no-untyped-def]
) -> None:
    """When a crash is detected, run_loop must report and then continue."""
    agent = make_agent()
    _stub_loop_phases(agent)
    # Seed a crashed lifecycle file (next_phase="contemplate" so the
    # resumed iteration reaches contemplate, which we assert below).
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps(
        {"iteration": 2, "next_phase": "contemplate"}
    )

    called = asyncio.Event()

    async def fake_handle_crash(event: CrashEvent, **_: Any) -> None:
        called.set()

    with (
        patch("maestro.crash.handle_crash", new=fake_handle_crash),
        patch("maestro.crash.build_crash_event", return_value=_make_event()),
    ):
        result = await agent.run_loop(
            max_iters=1,
            interval=0,
            task_every=0,
            evolve_every=0,
            dream_every=0,
        )

    assert called.is_set()
    # Loop continues after crash reporting
    assert result is True
    agent.contemplate.assert_awaited()


# ── startup wiring: reconstituted SDK context → build_crash_event ──


@pytest.mark.asyncio
async def test_startup_crash_feeds_sdk_context_to_event(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The reconstituted SDKError reaches ``build_crash_event(sdk_error=...)``.

    Without this wiring every field the crash unwind pays I/O to persist is
    read only by the rate-limit cooldown gate's ``.category`` glance, and the
    crash ping still describes the failure from the log tail alone.
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    # No ``last_shutdown`` → crash detected on startup.
    agent.config.lifecycle_path.write_text(
        json.dumps(
            {
                "iteration": 3,
                "next_phase": "contemplate",
                "last_exit_code": 1,
                "last_sdk_error": {
                    "message": "boom",
                    "category": "cli_crash",
                    "exit_code": 1,
                    "stderr_head": "ProcessError: 1",
                    "retries_attempted": 2,
                },
            }
        )
    )

    with patch("maestro.crash.handle_crash", AsyncMock()), \
         patch("maestro.crash.build_crash_event") as build:
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    build.assert_called_once()
    passed = build.call_args.kwargs["sdk_error"]
    assert passed is not None
    assert passed.category == ErrorCategory.CLI_CRASH
    assert passed.exit_code == 1
    assert passed.stderr == "ProcessError: 1"
    assert passed.retries_attempted == 2


@pytest.mark.asyncio
async def test_startup_crash_without_sdk_context_passes_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A crash with no persisted ``last_sdk_error`` passes ``sdk_error=None``."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.config.lifecycle_path.write_text(
        json.dumps({"iteration": 3, "next_phase": "contemplate"}),
    )

    with patch("maestro.crash.handle_crash", AsyncMock()), \
         patch("maestro.crash.build_crash_event") as build:
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    build.assert_called_once()
    assert build.call_args.kwargs["sdk_error"] is None


def test_diagnostic_stderr_no_debug_lines_normalises_newlines() -> None:
    from maestro import sdk

    assert sdk.diagnostic_stderr("a\r\nb\n") == "a\nb"


_TRACE_FRAME = "    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1555:16)\n"


def test_stderr_failure_tail_short_text_returned_whole() -> None:
    from maestro import sdk

    assert sdk.stderr_failure_tail("Error: x", 500) == "Error: x"


def test_stderr_failure_tail_keeps_error_header_before_long_trace() -> None:
    from maestro import sdk

    text = "noise\n" * 50 + "Error: connect ECONNREFUSED\n" + _TRACE_FRAME * 20
    result = sdk.stderr_failure_tail(text, 500)
    assert result.startswith("Error: connect ECONNREFUSED")
    assert len(result) == 500


def test_stderr_failure_tail_trailing_error_line_uses_plain_tail() -> None:
    from maestro import sdk

    text = "x" * 600 + "\nError: boom"
    assert sdk.stderr_failure_tail(text, 500) == "Error: boom"


def test_stderr_failure_tail_all_frames_uses_plain_tail() -> None:
    from maestro import sdk

    text = _TRACE_FRAME * 20
    result = sdk.stderr_failure_tail(text, 500)
    assert result == text[-500:].partition("\n")[2]
    assert result.startswith("    at ")


def test_stderr_failure_tail_cut_on_line_boundary_kept_whole() -> None:
    from maestro import sdk

    text = "    at a\n" + "    at " + "b" * 493
    assert text[-501] == "\n"
    assert sdk.stderr_failure_tail(text, 500) == text[-500:]


def test_stderr_failure_tail_single_long_line_kept_partial() -> None:
    from maestro import sdk

    text = "    at " + "z" * 600
    assert sdk.stderr_failure_tail(text, 500) == text[-500:]


def test_stderr_failure_tail_skips_node_uncaught_trailer() -> None:
    from maestro import sdk

    text = (
        "noise\n" * 50
        + "Error: connect ECONNREFUSED 127.0.0.1:443\n"
        + _TRACE_FRAME * 10
        + "{\n  errno: -111,\n  code: 'ECONNREFUSED',\n  syscall: 'connect'\n}\n"
        + "\nNode.js v20.11.0\n"
    )
    result = sdk.stderr_failure_tail(text, 500)
    assert result.startswith("Error: connect ECONNREFUSED")


def test_render_stderr_tail_node_trailer_keeps_header() -> None:
    from maestro.crash import _render_stderr_tail

    text = (
        "Error: connect ECONNREFUSED 127.0.0.1:443\n"
        + _TRACE_FRAME * 10
        + "{\n  errno: -111,\n  code: 'ECONNREFUSED'\n}\n\nNode.js v20.11.0\n"
    )
    assert "Error: connect" in _render_stderr_tail(text)


@pytest.mark.parametrize(
    "trailer",
    [
        "{\n  errno: -111,\n  cause: {\n    code: 'X'\n  },\n  port: 443\n}\n",
        "{\n  code: 'E',\n  hosts: [ 'a', 'b' ]\n}\n",
        "{\n  list: [\n    'a',\n    'b'\n  ],\n  port: 443\n}\n",
    ],
)
def test_render_stderr_tail_nested_trailer_keeps_header(trailer: str) -> None:
    from maestro.crash import _render_stderr_tail

    text = (
        "Error: connect ECONNREFUSED 127.0.0.1:443\n"
        + _TRACE_FRAME * 10
        + trailer
        + "\nNode.js v20.11.0\n"
    )
    assert "Error: connect" in _render_stderr_tail(text)


def test_stderr_failure_tail_fallback_skips_nested_trailer() -> None:
    """With no ``Error`` header, the trailer skip still passes nested closers."""
    from maestro import sdk

    text = (
        "noise\n" * 50
        + "fatal: connection refused\n"
        + _TRACE_FRAME * 10
        + "{\n  list: [\n    'a',\n    \"b\"\n  ],\n  cause: {\n    x: 1\n  },\n}\n"
    )
    assert sdk.stderr_failure_tail(text, 500).startswith("fatal: connection refused")


def test_persist_rebuild_render_all_debug_stream_keeps_tail(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An all-debug stream rendered after persist + rebuild keeps its tail."""
    from maestro.crash import _render_stderr_tail

    stderr = "\n".join(
        f"[DEBUG] line {i} some startup noise here padding" for i in range(40)
    )
    agent = make_agent()
    agent._persist_crash_exit_code(
        None,
        sdk_error=SDKError(
            "killed", category=ErrorCategory.CLI_CRASH, exit_code=137, stderr=stderr,
        ),
    )
    rebuilt = agent._read_crash_sdk_context()
    assert rebuilt is not None
    rendered = _render_stderr_tail(rebuilt.stderr or "")
    assert rendered == _render_stderr_tail(stderr)
    assert "[DEBUG] line 39 " in rendered


@pytest.mark.asyncio
async def test_handle_crash_rebuilt_error_followed_by_trace_keeps_header(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An ``Error:`` header followed by a long stack trace survives both caps."""
    agent = make_agent()
    agent._persist_crash_exit_code(
        None,
        sdk_error=SDKError(
            "died",
            category=ErrorCategory.NETWORK,
            exit_code=1,
            stderr=_DEBUG_NOISE_LINE * 5
            + "Error: connect ECONNREFUSED 127.0.0.1:443\n"
            + _TRACE_FRAME * 20,
        ),
    )
    kwargs = _make_handle_crash_args(mem_fs)
    event = _make_event()
    event.sdk_error = agent._read_crash_sdk_context()
    await handle_crash(event, **kwargs)

    assert "ECONNREFUSED" in _read_crash_milestone(mem_fs)


def test_persist_crash_scrubs_token_before_cap(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A credential the cap would cut mid-token is redacted before the cut.

    The anchor line holds the token so that a cap-first order keeps only
    ``ghp_`` plus 26 characters, too short for the redaction to match.
    """
    token = "ghp_" + "A" * 36
    agent = make_agent()
    agent._persist_crash_exit_code(
        None,
        sdk_error=SDKError(
            "died",
            category=ErrorCategory.NETWORK,
            exit_code=1,
            stderr=(
                "noise\n" * 5
                + "Error: auth failed "
                + "x" * 450
                + " "
                + token
                + "\n    at f (a.js:1:1)" * 5
            ),
        ),
    )
    rebuilt = agent._read_crash_sdk_context()
    assert rebuilt is not None
    assert "A" * 20 not in rebuilt.stderr
