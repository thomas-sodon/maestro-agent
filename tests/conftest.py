"""Shared test fixtures and mock helpers."""

from __future__ import annotations

import builtins as _builtins
import io
import logging
import os as _os
import subprocess as _subprocess_module
import sys
import tempfile as _tempfile
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from unittest.mock import MagicMock as _MagicMock

import anyio
import pytest
from mcp import ClientSession
from mcp.server import Server
from mcp.shared.memory import create_client_server_memory_streams

from maestro.config import Config
from maestro.pr_facts import MergeTruth
from maestro.sdk import QueryResult
from tests._fd_leak_guard import fd_leak_message, open_fd_count
from tests._live_state_fence import (
    LiveStateFence,
    format_test_failure,
    format_violation_report,
    install_fence,
    merge_worker_violations,
    resolve_live_state_roots,
    serialise_violations,
    session_failed,
    violations_for,
)

# Re-export QueryResult as MockQueryResult for backward compatibility with tests.
MockQueryResult = QueryResult

_LIVE_STATE_FENCE = pytest.StashKey[LiveStateFence]()


def pytest_configure(config: pytest.Config) -> None:
    """Install the enforcing live-state fence (proposal 138)."""
    fence = LiveStateFence(roots=resolve_live_state_roots(config.rootpath))
    config.stash[_LIVE_STATE_FENCE] = fence
    install_fence(fence)
    fence.armed = True
    fence.enforcing = True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None]:
    """Attribute live-state writes to the running test's nodeid."""
    fence = item.config.stash[_LIVE_STATE_FENCE]
    fence.current_nodeid = item.nodeid
    try:
        yield
    finally:
        fence.current_nodeid = None


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Relay violations from an xdist worker; elsewhere fail the run on any."""
    fence = session.config.stash[_LIVE_STATE_FENCE]
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput["live_state_violations"] = serialise_violations(fence.violations)
        return
    if session_failed(fence.violations) and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    """On the xdist controller, merge a finished worker's violations."""
    merge_worker_violations(
        node.config.stash[_LIVE_STATE_FENCE], getattr(node, "workeroutput", {}),
    )


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """List live-state writes, including those outside any test."""
    lines = format_violation_report(terminalreporter.config.stash[_LIVE_STATE_FENCE].violations)
    if lines:
        terminalreporter.section("live-state fence")
        for line in lines:
            terminalreporter.write_line(line)


@pytest.fixture(autouse=True)
def _live_state_verdict(request: pytest.FixtureRequest) -> Generator[None]:
    """Fail a test that wrote under a live-state root, even if the refusal was swallowed.

    pytest orders same-scope autouse fixtures by name (``parsefactories`` walks
    ``dir()``), so this sorts ahead of ``_own_sdk_runtime`` and its teardown runs
    after that fixture's; keep the name sorting first when adding autouse fixtures.
    """
    yield
    violations = violations_for(request.config.stash[_LIVE_STATE_FENCE], request.node.nodeid)
    if violations:
        pytest.fail(format_test_failure(violations), pytrace=False)


@pytest.fixture(autouse=True)
def _measure_fd_leak(request: pytest.FixtureRequest) -> Generator[None]:
    """Fail a test that leaves file descriptors open (issue #4213).

    Named to sort after ``_live_state_verdict`` but ahead of the ``_neutralize_*``
    / ``_own_*`` autouse fixtures, so their teardown has already closed what they
    opened by the time the count is re-read here. Browser tests are exempt: the
    session-scoped Playwright/server fixtures allocate descriptors lazily on the
    first test that uses them, which is not a per-test leak.
    """
    if request.node.get_closest_marker("browser") is not None:
        yield
        return
    before = open_fd_count()
    yield
    message = fd_leak_message(before, open_fd_count())
    if message:
        pytest.fail(message, pytrace=False)


@asynccontextmanager
async def connected_session(server: Server) -> AsyncIterator[ClientSession]:
    """Drive an in-process MCP ``Server`` over a real in-memory client session.

    A 2.x-native reproduction of the ``mcp`` 1.x
    ``mcp.shared.memory.create_connected_server_and_client_session`` helper — 2.x
    removed that helper, leaving only the lower-level
    ``create_client_server_memory_streams``. This wires the server and an ``mcp``
    ``ClientSession`` through a pair of in-memory streams (no socket, no
    subprocess) and dispatches real ``list_tools`` / ``call_tool`` requests the
    way the SDK client does, so the shim's wire-contract and dispatch tests can
    assert on the client-side result instead of reaching into private server
    internals.
    """
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                lambda: server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                yield client
            task_group.cancel_scope.cancel()


def capture_stdout(func: Any, *args: Any, **kwargs: Any) -> str:
    """Capture stdout from a function call, swallowing SystemExit."""
    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        func(*args, **kwargs)
    except SystemExit:
        pass
    finally:
        sys.stdout = old
    return buf.getvalue()


def make_mock_query(response: str) -> AsyncMock:
    """Create a mock for claude_agent_sdk.query that returns a fixed response."""
    mock = AsyncMock(return_value=QueryResult(response=response))
    return mock


class _AppendBuffer(io.StringIO):
    """StringIO that appends to in-memory storage on close."""

    def __init__(self, storage: dict[str, str], path: str, existing: str) -> None:
        super().__init__()
        self._storage = storage
        self._path = path
        self._existing = existing

    def close(self) -> None:
        if not self.closed:
            self._storage[self._path] = self._existing + self.getvalue()
        super().close()


class _WriteBuffer(io.StringIO):
    """StringIO that writes to in-memory storage on close."""

    def __init__(self, storage: dict[str, str], path: str) -> None:
        super().__init__()
        self._storage = storage
        self._path = path

    def close(self) -> None:
        if not self.closed:
            self._storage[self._path] = self.getvalue()
        super().close()


class _BinaryWriteBuffer(io.BytesIO):
    """BytesIO that decodes content to UTF-8 and persists on close."""

    def __init__(self, storage: dict[str, str], path: str) -> None:
        super().__init__()
        self._storage = storage
        self._path = path

    def close(self) -> None:
        if not self.closed:
            try:
                self._storage[self._path] = self.getvalue().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "mem_fs binary mode requires UTF-8-decodable content "
                    f"(path={self._path!r}, storage is dict[str, str])"
                ) from exc
        super().close()


class _BinaryAppendBuffer(io.BytesIO):
    """BytesIO that prepends existing content, decodes, and persists on close."""

    def __init__(self, storage: dict[str, str], path: str, existing: str) -> None:
        super().__init__()
        self._storage = storage
        self._path = path
        self._existing_encoded = existing.encode("utf-8")

    def close(self) -> None:
        if not self.closed:
            combined = self._existing_encoded + self.getvalue()
            try:
                self._storage[self._path] = combined.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    "mem_fs binary mode requires UTF-8-decodable content "
                    f"(path={self._path!r}, storage is dict[str, str])"
                ) from exc
        super().close()


@pytest.fixture(autouse=True)
def _block_real_sdk_transport(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fail a test whose SDK call reaches the real Claude CLI transport.

    Every ``claude_agent_sdk``-backed path — ``query``, ``query_on_session``,
    ``single_use_call``, ``ClaudeSDKClient`` — spawns the CLI through
    ``SubprocessCLITransport.connect``. A ``patch("maestro.<mod>.query")`` that
    misses the module actually making the call (e.g. after a body moves to a
    sibling module) would otherwise open a real session. The attempt raises, and
    is also recorded so a retry loop or broad handler that swallows the raise
    still fails the test at teardown. The ``cli_paced`` provider drives the CLI
    through ``maestro_sdk``'s pty runner instead and is not fenced here.
    """
    if request.node.get_closest_marker("browser") is not None:
        yield
        return
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    attempts: list[str] = []

    async def _refuse(self: Any) -> None:
        attempts.append(request.node.nodeid)
        raise RuntimeError(
            "real Claude CLI transport reached in a unit test — patch the SDK "
            "entry point in the module that makes the call",
        )

    monkeypatch.setattr(SubprocessCLITransport, "connect", _refuse)
    yield
    if attempts:
        pytest.fail(
            f"{len(attempts)} real SDK transport connect attempt(s) — an SDK "
            "patch missed the module that made the call",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _fake_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure auth credentials are always present so check_secrets() passes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    # Pinned empty (not unset) so a developer's ``.env`` OAuth token, which
    # ``load_dotenv(override=False)`` would otherwise inject, cannot make the
    # run look dual-credentialed; every reader treats "" as absent.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")


@pytest.fixture(autouse=True)
def _neutralize_gate_lease() -> Iterator[None]:
    """Keep the gate-execution lease off the real host in unit tests.

    ``gate_lease_enabled`` defaults on (proposal 126, graduated to fix #3760), so
    any test calling a gate reader (``run_quality_gates`` / ``run_gate_check`` /
    ``preflight`` / the ``gate-lease`` wrapper) with a default ``Config`` now
    routes through the real lease seam: :func:`gate_capacity.measure_host_capacity`
    reads ``/proc/meminfo`` + ``/sys/fs/cgroup`` + ``os.sched_getaffinity``, and
    :func:`gate_capacity.acquire_slot` ``os.makedirs`` + ``fcntl.flock`` the
    *shared* ``~/.maestro/gate_slots`` dir — real OS/FS I/O the Testing Principles
    forbid, and, shared across sibling checkouts and xdist workers, contention can
    enter ``acquire_slot``'s bounded ``time.sleep`` wait and flake the 5s
    ``pytest-timeout``. Patch both impurities to host-free stand-ins: a fixed
    ``HostCapacity`` and an ``acquire_slot`` that fails open (``"fallback"`` — no
    ``count_in_flight`` probe, no lock dir), so a routed reader still exercises the
    real ``plan_lease`` / ``worker_args`` policy but yields the one-worker floor,
    whose ``worker_args`` is ``()`` — argv byte-identical to the flag-off path.

    The module that tests these primitives directly (``test_gate_capacity``)
    overrides this fixture with a same-named no-op; tests exercising the lease from
    a consumer patch ``lease_for_gate_run`` (or ``measure_host_capacity`` /
    ``acquire_slot``) themselves, winning inside their own ``with``.
    """
    from maestro import gate_capacity

    @contextmanager
    def _fallback_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "fallback"

    with (
        patch.object(
            gate_capacity,
            "measure_host_capacity",
            return_value=gate_capacity.HostCapacity(cores=2, available_mib=4096),
        ),
        patch.object(gate_capacity, "acquire_slot", _fallback_acquire),
    ):
        yield


@pytest.fixture(autouse=True)
def _neutralize_runtime_contract() -> Iterator[None]:
    """Keep the runtime-contract probe off the real host in unit tests.

    ``preflight`` and ``_build_agent`` fold ``deploy/runtime_contract.toml``
    against the live environment (``/proc``, cgroups, PATH, ``os.access``).
    Patch ``contract_violations`` to a clean verdict; the module testing the
    enforcement itself overrides this fixture with a same-named no-op.
    """
    from maestro import runtime_contract

    with patch.object(runtime_contract, "contract_violations", return_value=()):
        yield


@contextmanager
def _sdk_runtime_scope() -> Iterator[None]:
    """Snapshot SDK, provider and ``maestro`` logger state; restore it on exit.

    Handlers added inside the scope are removed and closed; handlers removed
    inside it are re-attached. See :func:`_own_sdk_runtime`.
    """
    from maestro import sdk
    from maestro.providers import registry

    sdk_snapshot = sdk.snapshot_sdk_runtime()
    provider_snapshot = registry.snapshot_provider_runtime()
    maestro_logger = logging.getLogger("maestro")
    handlers_before = list(maestro_logger.handlers)
    level_before = maestro_logger.level
    try:
        yield
    finally:
        sdk.restore_sdk_runtime(sdk_snapshot)
        registry.restore_provider_runtime(provider_snapshot)
        for handler in list(maestro_logger.handlers):
            if handler not in handlers_before:
                maestro_logger.removeHandler(handler)
                handler.close()
        for handler in handlers_before:
            if handler not in maestro_logger.handlers:
                maestro_logger.addHandler(handler)
        maestro_logger.setLevel(level_before)


@pytest.fixture(autouse=True)
def _own_sdk_runtime() -> Iterator[None]:
    """The one owner of cross-test SDK state (proposal 138).

    ``maestro.sdk`` and ``maestro.providers.registry`` hold process-global sinks
    (cost backend, SDK log dir, usage sink, model defaults, provider selection)
    that ``cli.main`` configures from the real TOML chain, whose ``memory_dir``
    resolves to the live ``data/``. ``mem_fs`` only covers the test that did the
    configuring; without this fence every later test in the process would write
    real cost rows and SDK logs. Restoring each module's ``_RUNTIME_GLOBALS``
    around every test bounds a leaked sink to its own test. This fixture is the
    sole restorer of that state — ad-hoc ``_reset_*_for_tests`` hooks are
    forbidden by ``tests/test_sdk_runtime_owner.py``.
    """
    with _sdk_runtime_scope():
        yield


@pytest.fixture(autouse=True)
def _block_external_commands(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block gh calls and git calls targeting the real repo in unit tests.

    Guards subprocess.run, call, check_call, check_output, and Popen.
    Allows git calls when cwd is under the system temp directory
    (test fixtures use tmp_path). Skips entirely for browser tests
    (which drive a real Chromium process). Component tests run through
    this guard; MockVCS delegates git to a real repo inside tmp_path.
    """
    markers = {m.name for m in request.node.iter_markers()}
    if markers & {"browser"}:
        return

    _tmp_prefixes = (
        _tempfile.gettempdir(),
        _os.path.realpath(_tempfile.gettempdir()),
        "/tmp",
        "/private/tmp",
    )

    def _make_guard(original: Any) -> Any:
        def _guarded(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(cmd, str):
                program = _os.path.basename(cmd.split()[0]) if cmd.strip() else ""
            elif isinstance(cmd, (list, tuple)) and cmd:
                program = _os.path.basename(str(cmd[0]))
            else:
                return original(cmd, *args, **kwargs)

            if program == "gh":
                raise RuntimeError(
                    f"Unit test called gh without mocking subprocess: {cmd!r}. "
                    "Mock subprocess or use component tests."
                )
            if program == "git":
                cwd = str(kwargs.get("cwd") or "")
                if not cwd.startswith(_tmp_prefixes):
                    raise RuntimeError(
                        f"Unit test called git outside tmp dir without mocking "
                        f"subprocess: {cmd!r} (cwd={cwd!r}). "
                        "Mock subprocess or use component tests."
                    )
            return original(cmd, *args, **kwargs)
        return _guarded

    for attr in ("run", "call", "check_call", "check_output", "Popen"):
        original = getattr(_subprocess_module, attr)
        monkeypatch.setattr(_subprocess_module, attr, _make_guard(original))


# Fake descriptors ``mem_fs`` returns from ``os.open`` / ``tempfile.mkstemp``.
_MEM_FS_OPEN_FD = 998
_MEM_FS_MKSTEMP_FD = 999


@pytest.fixture
def mem_fs() -> Generator[dict[str, str]]:
    """In-memory filesystem that intercepts all Path and open() operations.

    Yields a dict mapping path strings to file contents, allowing tests
    to inspect or pre-populate file state without touching the real filesystem.
    Source code reads (e.g., HTML templates) fall through to the real filesystem.
    """
    files: dict[str, str] = {}
    _real_open = _builtins.open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        path = str(file)
        if "b" in mode:
            if "__pycache__" in path:
                # Import machinery (e.g. pytest's assertion rewriter caching a
                # plugin module first imported lazily under mem_fs) writes
                # bytecode, not test state; binary .pyc can't live in dict[str, str].
                return _real_open(file, mode, *args, **kwargs)
            if "w" in mode:
                return _BinaryWriteBuffer(files, path)
            elif "a" in mode:
                return _BinaryAppendBuffer(files, path, files.get(path, ""))
            else:
                # Binary read ("rb")
                if path in files:
                    return io.BytesIO(files[path].encode("utf-8"))
                # Fall through for source code reads (binary mode is rare here).
                return _real_open(file, mode, *args, **kwargs)
        elif "a" in mode:
            return _AppendBuffer(files, path, files.get(path, ""))
        elif "w" in mode:
            return _WriteBuffer(files, path)
        else:
            if path in files:
                return io.StringIO(files[path])
            # Fall through for source code reads (e.g., HTML templates)
            return _real_open(file, mode, *args, **kwargs)

    def fake_exists(self: Path) -> bool:
        if str(self) in files:
            return True
        return _os.path.exists(str(self))

    def fake_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        path = str(self)
        if path in files:
            return files[path]
        # Fall through for source code (e.g., templates)
        with _real_open(path) as f:
            return f.read()

    def fake_write_text(
        self: Path, content: str, encoding: str | None = None, errors: str | None = None,
    ) -> None:
        files[str(self)] = content

    def fake_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        pass

    def fake_replace(self: Path, target: Any) -> None:
        data = files.pop(str(self), "")
        files[str(target)] = data

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        path = str(self)
        if not missing_ok and path not in files:
            raise FileNotFoundError(path)
        files.pop(path, None)

    def fake_mkstemp(
        suffix: str = "", prefix: str = "tmp", dir: Any = None, text: bool = False,
    ) -> tuple[int, str]:
        tmp_path = f"{dir}/{prefix}tmp{suffix}"
        return _MEM_FS_MKSTEMP_FD, tmp_path

    _real_close = _os.close

    def fake_close(fd: int) -> None:
        # Swallow only the fake descriptors this fixture hands out; a real one
        # (an event loop's ``can_use_pidfd`` probe, a pipe) must still close, or
        # every ``mem_fs`` test leaks it and a long run hits EMFILE (#4213).
        if fd in (_MEM_FS_MKSTEMP_FD, _MEM_FS_OPEN_FD):
            return
        _real_close(fd)

    # Drop the cost-tracking module-level TTL cache so a stale snapshot from
    # an earlier test (whose ``mem_fs`` is now torn down) cannot leak into
    # this test's view of the file.  ``mem_fs`` does not move ``mtime``
    # forward, so the cache key alone is not enough to invalidate.
    from maestro.cost_tracking import _clear_cost_rate_cache
    _clear_cost_rate_cache()

    with patch("builtins.open", side_effect=fake_open), \
         patch.object(Path, "exists", fake_exists), \
         patch.object(Path, "read_text", fake_read_text), \
         patch.object(Path, "write_text", fake_write_text), \
         patch.object(Path, "mkdir", fake_mkdir), \
         patch.object(Path, "replace", fake_replace), \
         patch.object(Path, "unlink", fake_unlink), \
         patch("tempfile.mkstemp", side_effect=fake_mkstemp), \
         patch("os.close", side_effect=fake_close), \
         patch("os.open", return_value=_MEM_FS_OPEN_FD), \
         patch("fcntl.flock"):
        yield files
    _clear_cost_rate_cache()


@pytest.fixture
def make_agent(mem_fs: dict[str, str]):  # type: ignore[no-untyped-def]
    """Factory fixture to build a MaestroAgent with in-memory filesystem."""
    from unittest.mock import MagicMock

    from maestro.agent import MaestroAgent
    from maestro.consciousness import Consciousness
    from maestro.contributions import ContributionTracker
    from maestro.evolution import EvolutionEngine
    from maestro.executor import Executor
    from maestro.journal import Journal
    from maestro.memory import Memory
    from maestro.notifications.base import NullNotifier
    from maestro.planner import Planner

    def _make() -> MaestroAgent:
        d = Path("/fake/agent")
        config = Config(
            memory_dir=d,
            workspace=d,
            self_review_max_rounds=0,
            # Pin session reuse OFF for unit tests. NOTE: the production default
            # is True (see Config.enable_session_reuse in maestro/config.py), so
            # this is a deliberate fixture override, not a restatement of the
            # default. Session-reuse tests therefore restate their precondition
            # explicitly via ``agent.config.enable_session_reuse = True/False``;
            # those per-test reassignments are intentional self-documentation —
            # do not "clean them up" as redundant.
            enable_session_reuse=False,
            uncalled_symbol_gate_enabled=False,
            # Pin the observed ready-and-waiting dequeue gate OFF for unit tests.
            # NOTE: the production default is True (see
            # Config.observed_ready_standdown_enabled) — this is a deliberate
            # fixture override, not a restatement of the default. The fixture's
            # code_host stubs read every make_agent PR as CI-green / 0-threads /
            # 0-overflow / not-behind / OPEN, i.e. ``ready_and_waiting``; with the
            # gate on, that would evict every fixture-based remediation at the
            # dequeue before it reaches the live path. Ready-gate tests restate
            # their precondition explicitly (their own stub Config, or
            # ``agent.config.observed_ready_standdown_enabled = True``).
            observed_ready_standdown_enabled=False,
        )
        config.ensure_dirs()
        memory = Memory(d)
        consciousness = Consciousness(d)
        journal = Journal(d)
        notifier = NullNotifier()
        executor = Executor()
        planner = Planner()
        evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                    config=config)
        # Mock ensure_main_branch by default — previously the loop's
        # phase-level except swallowed the "git called outside tmp dir"
        # error this fixture triggered. Now that phase errors propagate
        # loudly, tests that exercise run_loop() without caring about
        # branch setup need this to be a no-op. Individual tests can
        # override it via agent.evolution.ensure_main_branch = ... or
        # patch.object(...).
        evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
        # Same rationale for the strict mutating-phase precondition (#3711):
        # this fixture's VCS is a mock whose tree state is undefined, so the
        # refusal would fire in every test that merely drives a phase. Tests
        # exercising the guard itself override it.
        evolution.assert_clean_tree_for_mutation = (  # type: ignore[method-assign]
            MagicMock()
        )
        evolution.assert_on_main_for_mutation = (  # type: ignore[method-assign]
            MagicMock()
        )
        # The failed-plan retry door asks the host for a PR already on the
        # evolve branch (#4553); the fixture's real code host has no repo and
        # would defer every failed-plan retry. Default to "no such PR"; the
        # bind tests override it.
        evolution.find_existing_pr = MagicMock(  # type: ignore[method-assign]
            return_value=None,
        )
        # tend_evolution_prs is also neutralised by default — its real
        # body calls ``self._vcs.fetch("origin")`` which hits the real
        # SubprocessVCS bound to ``/fake/agent`` and trips the subprocess
        # guard. Tests that exercise the phase's own behaviour should
        # patch it explicitly or use the dedicated component fixtures.
        # Proposal-124 attribution (#3793) reads the branch's base→HEAD diff off
        # the VCS rather than the plan's declared ``files_affected``; the real
        # ``SubprocessVCS`` bound to ``/fake/agent`` would trip the subprocess
        # guard. Shadow only those two read-only methods, keeping the fixture's
        # long-standing convention that ``file.py`` is "in this branch's diff"
        # (``_seed_approved_plan``'s path) and every other path is the base's.
        # Tests that need a different diff override them explicitly.
        #
        # ``get_head_hash`` has a second in-tree reader: ``_observe_baseline``
        # runs the gate only when HEAD already equals the base sha it was handed.
        # The sentinel below matches no sha any test passes, so that seam keeps
        # returning ``None`` without a subprocess — the same answer it gave
        # before this shadow existed. A test that needs the baseline to be
        # *captured* must therefore stub ``_observe_baseline`` itself (they all
        # do) rather than expect the HEAD comparison to line up.
        evolution._vcs.get_head_hash = MagicMock(  # type: ignore[method-assign]
            return_value="fixturehead",
        )
        evolution._vcs.changed_paths = MagicMock(  # type: ignore[method-assign]
            return_value=("file.py",),
        )
        # ``implement_plan`` reads the uncapped cumulative diff unconditionally
        # for the register-key refusal (proposal 152); an empty diff sets no key.
        evolution._vcs.get_diff = MagicMock(  # type: ignore[method-assign]
            return_value="",
        )
        # ...diffed from ``merge_base(origin/main, HEAD)``; ``None`` (unresolved)
        # falls back to the implementation base without a subprocess.
        evolution._vcs.merge_base = MagicMock(  # type: ignore[method-assign]
            return_value=None,
        )
        from maestro.evolution_tend import TendPRsResult
        evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
            return_value=TendPRsResult(),
        )
        # The pre-execution PR-merge-block gate and ``_probe_pr_merge_block``
        # reach ``evolution.code_host``; the real ``SubprocessCodeHost`` would
        # shell out to ``gh``. Shadow only the three read-only methods the probe
        # uses (leaving ``create_pr``/``push`` real, since several tests drive
        # those through a globally patched ``subprocess.run``) so the gate is a
        # no-op (reason ``NONE``) unless a test configures the host explicitly.
        evolution._host.is_pr_merged = MagicMock(  # type: ignore[attr-defined]
            return_value=False,
        )
        # The fail-loud merge read the ``is_pr_settled`` chokepoint and
        # ``_host_or_git_pr_merged`` wire (#2954) — shadowed too, since the real
        # method shells out to ``gh`` and would trip the subprocess guard, and a
        # raise there would divert every fixture-based merge read onto the
        # git-subject fallback. It *delegates* to whatever ``is_pr_merged`` is at
        # call time rather than pinning its own verdict, so a test that overrides
        # only the best-effort stub (the established idiom, used by dozens of
        # tests) still steers the strict oracle to the same answer.
        evolution._host.is_pr_merged_strict = MagicMock(  # type: ignore[attr-defined]
            side_effect=lambda pr_num: evolution._host.is_pr_merged(pr_num),
        )
        evolution._host.is_pr_closed = MagicMock(  # type: ignore[attr-defined]
            return_value=False,
        )
        evolution._host.get_pr_check_conclusion = (  # type: ignore[attr-defined]
            MagicMock(return_value="success")
        )
        # ``_observe_pr_check_runs`` (red-root remote-CI read) fetches the PR's
        # failing check names off the host; the real method shells out to ``gh``.
        # Default to "no named failing check" so the red-root path stays a no-op
        # (classifies RUNNING) unless a test configures the host explicitly.
        evolution._host.get_pr_failing_check_names = (  # type: ignore[attr-defined]
            MagicMock(return_value=[])
        )
        evolution._host.get_open_evolution_pr_states = (  # type: ignore[attr-defined]
            MagicMock(return_value={})
        )
        # ``_record_remediation_outcome`` and the §4d completeness gate check
        # remaining eligible review items for open PRs (#2663): inline threads
        # plus body-overflow findings. Default all three reads to "nothing
        # outstanding" so existing tests are unaffected; tests exercising the
        # no-op / completeness paths override explicitly. ``get_pr_review_bodies``
        # / ``get_pr_comment_bodies`` must be stubbed too — the real
        # ``SubprocessCodeHost`` shells out to ``gh`` and would trip the
        # subprocess guard.
        evolution._host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
            return_value=[],
        )
        evolution._host.get_pr_review_bodies = MagicMock(  # type: ignore[attr-defined]
            return_value=[],
        )
        evolution._host.get_pr_comment_bodies = MagicMock(  # type: ignore[attr-defined]
            return_value=[],
        )
        # The fail-loud ``*_strict`` siblings (#4065) delegate to the best-effort
        # stubs at call time, so a test overriding a best-effort read also
        # drives the strict read the completeness gate and overflow scan use.
        _host = evolution._host
        _host.get_pr_review_threads_strict = MagicMock(  # type: ignore[attr-defined]
            side_effect=lambda n: _host.get_pr_review_threads(n),
        )
        _host.get_pr_review_bodies_strict = MagicMock(  # type: ignore[attr-defined]
            side_effect=lambda n: _host.get_pr_review_bodies(n),
        )
        _host.get_pr_comment_bodies_strict = MagicMock(  # type: ignore[attr-defined]
            side_effect=lambda n: _host.get_pr_comment_bodies(n),
        )
        # The salvage oracle's budget dimension (``_assess_pr_salvage``) reads the
        # commits-behind count off ``evolution._observe_pr_staleness``, whose real
        # body shells out to ``git`` via evolution's own SubprocessVCS bound to
        # ``/fake/agent`` and trips the subprocess guard. Default it to a
        # zero-behind observation (within the reconcile budget → budget-aware
        # disposal never fires) so existing salvage tests are unaffected; tests
        # exercising the past-budget DISPOSE path override it explicitly.
        evolution._observe_pr_staleness = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(commits_behind=0),
        )
        contributions = ContributionTracker(d, d)
        vcs = MagicMock()
        vcs.has_changes.return_value = False
        # ``commits_ahead_behind`` is unpacked into a 2-tuple by callers
        # (e.g. ``_probe_pr_merge_block``); an unconfigured MagicMock is not
        # iterable, so give a benign even default. ``rev_parse`` returns a
        # truthy sha so ``origin/<branch>`` resolves (no spurious AWAITING_PUSH).
        vcs.commits_ahead_behind.return_value = (0, 0)
        # ``run``'s step-5 clean-tree keep arm compares this count against 0;
        # an unconfigured MagicMock is not an int. Default to a measured zero
        # (no commits of the task's own → branch deleted as before); tests
        # exercising the keep arm set it positive, and ``None`` (probe failed)
        # is what falls back to ``commits_ahead_behind``.
        vcs.non_merge_commits_ahead.return_value = 0
        vcs.lap_commits_since.return_value = 0
        # The chosen-stand-down ahead-of-origin guard reads this rev-list count;
        # an unconfigured MagicMock is not an int. Default to 0 (branch not ahead
        # → EXTERNAL), preserving the pre-guard write path; tests exercising the
        # owned-ahead refusal set it positive and mock ``_fetch_origin_for_merge_truth``.
        vcs.commits_not_on_remote.return_value = 0
        vcs.rev_parse.return_value = "0" * 40
        # ``_probe_pr_merge_block`` runs a local merge-tree probe on a
        # BEHIND_MAIN verdict; an unconfigured MagicMock returns a truthy mock
        # (which would spuriously reclassify CONFLICTING). Default to no conflict
        # so the probe is a no-op unless a test configures it explicitly.
        vcs.merge_tree_conflicts.return_value = False
        # The register-key check diffs from ``merge_base(origin/main, HEAD)``;
        # ``None`` (unresolved) falls back to the implementation base.
        vcs.merge_base.return_value = None

        agent = MaestroAgent(
            config=config,
            consciousness=consciousness,
            journal=journal,
            memory=memory,
            executor=executor,
            planner=planner,
            evolution=evolution,
            notifier=notifier,
            contributions=contributions,
            vcs=vcs,
        )
        agent._probe_rate_limit = AsyncMock()  # type: ignore[method-assign]
        # The admission-face merged-PR guard in
        # ``_route_pr_feedback_to_pending_requests`` calls
        # ``_branch_merged_into_main`` → ``is_branch_merged_into_main`` which
        # runs ``git merge-base`` against ``/fake/agent`` and trips the
        # subprocess guard. Default it to "not merged" (admit) so existing
        # routing tests behave as before; tests exercising the guard override
        # the return value explicitly.
        agent._branch_merged_into_main = MagicMock(  # type: ignore[method-assign]
            return_value=False,
        )
        # The PR-servicing faces — the dequeue-face merged-PR exclusion
        # (``_run_task_phase`` → ``gate_merged``), the admission-face guard in
        # ``_route_pr_feedback_to_pending_requests``, and the task-entry
        # stand-down (``_preflight_merge_truth_standdown``) — all read merge
        # state through the ``is_pr_settled`` chokepoint (proposal 087), which
        # would ``git fetch origin`` (check=True) and call the host API at
        # ``/fake/agent`` and trip the subprocess guard. Default it to an
        # unsettled OPEN observation so existing remediation/routing tests reach
        # the live path; tests exercising the chokepoint override it explicitly.
        agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
            return_value=MergeTruth(
                pr_number=0,
                settled=False,
                state="OPEN",
                branch_landed=False,
                source="host_api",
                observed_at=0.0,
            ),
        )
        # The dequeue-face commit-ancestry gate (``_gate_pr_remediation_landed``)
        # runs ``git fetch origin`` (check=True) as its first physical act, which
        # would trip the subprocess guard at ``/fake/agent``. Default it to "not
        # landed" (fall through) so existing remediation tests reach the live
        # path; tests exercising the gate override or unpatch it explicitly.
        agent._gate_pr_remediation_landed = MagicMock(  # type: ignore[method-assign]
            return_value=False,
        )
        # The verdict-fetch action-zero (``_record_remediation_fetch_truth``)
        # runs ``git fetch origin`` (check=True) at ``config.workspace`` and
        # would trip the subprocess guard. Default it to a no-subprocess stand-in
        # that still records the per-task fetch-truth marker (via the real
        # ``record_fetch_truth``) so the §4e verdict-fetch gate sees git as having
        # spoken and existing remediation tests are unaffected. Tests exercising
        # the downgrade override it to a non-recording mock; the ordering test
        # unpatches it explicitly.
        def _fake_fetch_truth(
            task_id: str, pr_num: int, pr_branch: str,
        ) -> bool:
            from maestro.remediation_tracker import record_fetch_truth
            record_fetch_truth(task_id, pr_num, False, config.memory_dir)
            return False
        agent._record_remediation_fetch_truth = MagicMock(  # type: ignore[method-assign]
            side_effect=_fake_fetch_truth,
        )
        return agent

    return _make


def arm_entry_clean_tree(vcs: _MagicMock, *, later: bool = True) -> None:
    """Arm *vcs* for ``EvolutionEngine.implement()``'s entry preconditions.

    The entry probes read the tree through ``porcelain_status()`` and the
    branch through ``get_current_branch()`` before any branch is cut (#3711).
    A bare ``MagicMock`` returns a truthy sentinel for both, so every test that
    merely drives ``implement()`` would hit the refusal; this arms them as a
    clean ``main``.

    Deliberately disjoint from ``has_changes()``, which the commit tail reads:
    the entry probe consumes none of a test's ``has_changes`` sequence, so an
    extra entry read can never shift one. *later* is the convenience default
    for that tail read, and is ignored once a test pins its own sequence.

    ``get_current_branch`` is re-read *after* ``ensure_main_branch`` has
    positioned HEAD, which a mock cannot simulate; so when the test has not
    pinned it to a string of its own, default it to ``"main"``.
    """
    vcs.porcelain_status.return_value = ""
    if vcs.has_changes.side_effect is None:
        vcs.has_changes.return_value = later
    if not isinstance(vcs.get_current_branch.return_value, str):
        vcs.get_current_branch.return_value = "main"
