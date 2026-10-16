"""Runtime contract enforced at preflight and startup (proposal 141 step 8)."""

from __future__ import annotations

import argparse
import io
import logging
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from maestro import runtime_contract
from maestro.cli import _build_agent, cmd_preflight
from maestro.config import Config
from maestro.runtime_contract import (
    ContractEntry,
    ContractError,
    ContractKind,
    Observations,
    RuntimeContract,
    contract_violations,
    enforce_runtime_contract,
)

_FAKE_DIR = Path("/fake/contract")


@pytest.fixture(autouse=True)
def _neutralize_runtime_contract() -> Iterator[None]:
    """Override conftest's neutralizer: this module exercises the real probe."""
    yield


def _config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "memory_dir": _FAKE_DIR,
        "workspace": _FAKE_DIR,
        "enable_session_reuse": False,
        "publication_enabled": False,
    }
    defaults.update(overrides)
    return Config(**defaults)


def _entry(kind: str, name: str, **kw: Any) -> ContractEntry:
    kw.setdefault("required_when", ("always",))
    kw.setdefault("provision", "apt install it")
    return ContractEntry(kind=cast(ContractKind, kind), name=name, **kw)


_CONTRACT = RuntimeContract(entries=(
    _entry("binary", "npx", provision="install nodejs"),
    _entry("launcher_package", "pkg", launcher="npx"),
))


def _obs(binaries: frozenset[str]) -> Observations:
    return Observations(
        found_binaries=binaries,
        resolved_distributions=frozenset(),
        writable_paths=frozenset(),
        facts={},
        identity=(0, 0, "/root"),
    )


def _patched(binaries: frozenset[str]) -> Any:
    return (
        patch.object(runtime_contract, "load_contract", return_value=_CONTRACT),
        patch.object(runtime_contract, "observe", return_value=_obs(binaries)),
    )


_MISSING = frozenset[str]()
_PRESENT = frozenset({"npx"})


# ── contract_violations ──────────────────────────────────────────────


def test_contract_violations_satisfied_contract_is_empty() -> None:
    load, obs = _patched(_PRESENT)
    with load, obs:
        assert contract_violations(_config(), None) == ()


def test_contract_violations_missing_binary_names_entry() -> None:
    load, obs = _patched(_MISSING)
    with load, obs as observe:
        lines = contract_violations(_config(), "production")
    assert lines == ("binary npx: not on PATH; provision: install nodejs",)
    assert observe.call_args.kwargs["network"] is False


def test_contract_violations_launcher_package_stays_unverified() -> None:
    """An offline probe never resolves a package, so it is never a violation."""
    captured: list[runtime_contract.ContractVerdict] = []
    real_evaluate = runtime_contract.evaluate

    def _spy(*args: Any, **kwargs: Any) -> runtime_contract.ContractVerdict:
        verdict = real_evaluate(*args, **kwargs)
        captured.append(verdict)
        return verdict

    load, obs = _patched(_PRESENT)
    with load, obs as observe, patch.object(runtime_contract, "evaluate", _spy):
        lines = contract_violations(_config(), None)
    assert lines == ()
    assert observe.call_args.kwargs["network"] is False
    package = [f for f in captured[0].findings if f.entry.kind == "launcher_package"]
    assert len(package) == 1
    assert package[0].status == "unverified"


@pytest.mark.parametrize(
    "text",
    [
        "[[entry]\nname = ",  # TOML syntax error
        "entry = [1, 2]\n",  # entries that are not tables
        "entry = 3\n",  # entry that is not an array
    ],
)
def test_contract_violations_malformed_contract_is_one_line(text: str) -> None:
    with patch.object(Path, "read_text", return_value=text):
        lines = contract_violations(_config(), None)
    assert len(lines) == 1
    assert lines[0].startswith("runtime contract: runtime contract")


@pytest.mark.parametrize(
    "exc",
    [PermissionError("denied"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")],
)
def test_contract_violations_unreadable_contract_is_one_line(exc: Exception) -> None:
    with patch.object(Path, "read_text", side_effect=exc):
        lines = contract_violations(_config(), None)
    assert len(lines) == 1
    assert "unreadable" in lines[0]


def test_preflight_malformed_contract_flag_off_continues(
    mem_fs: dict[str, str],
) -> None:
    with patch.object(
        runtime_contract, "load_contract",
        side_effect=ContractError("malformed"),
    ):
        output, code = _run_preflight(_config())
    assert code == 0
    assert "runtime_contract: 1 warning(s)" in output


def test_preflight_enabled_launcher_missing_npx_owned_by_launcher_check(
    mem_fs: dict[str, str],
) -> None:
    """Until step 9, a missing launcher for an enabled server fails check 2b first."""
    enforce = patch.object(runtime_contract, "enforce_runtime_contract")
    which = patch("shutil.which", return_value=None)
    with enforce as contract, which:
        output, code = _run_preflight(_config(context7_mcp_enabled=True))
    assert code == 1
    assert "'npx' was not found on PATH" in output
    contract.assert_not_called()


def test_build_agent_enabled_launcher_missing_refused_before_contract(
    mem_fs: dict[str, str],
) -> None:
    """``_build_agent`` likewise refuses a missing launcher before the contract."""
    enforce = patch.object(runtime_contract, "enforce_runtime_contract")
    which = patch("shutil.which", return_value=None)
    with enforce as contract, which, pytest.raises(SystemExit) as exc:
        _build_agent(_config(context7_mcp_enabled=True))
    assert "'npx' was not found on PATH" in str(exc.value)
    contract.assert_not_called()


def test_contract_violations_contract_error_is_one_line() -> None:
    with patch.object(
        runtime_contract, "load_contract", side_effect=ContractError("not found"),
    ):
        lines = contract_violations(_config(), None)
    assert lines == ("runtime contract: not found",)


# ── enforce_runtime_contract ─────────────────────────────────────────


def test_enforce_raises_naming_entry_when_enforced() -> None:
    load, obs = _patched(_MISSING)
    with load, obs, pytest.raises(SystemExit) as exc:
        enforce_runtime_contract(_config(), enforced=True, env_name=None)
    assert "binary npx" in str(exc.value)


def test_enforce_warns_and_returns_when_not_enforced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    load, obs = _patched(_MISSING)
    with load, obs, caplog.at_level(logging.WARNING, "maestro.runtime_contract"):
        lines = enforce_runtime_contract(_config(), enforced=False, env_name=None)
    assert len(lines) == 1
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert "binary npx" in warned[0].getMessage()


def test_enforce_clean_contract_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    load, obs = _patched(_PRESENT)
    with load, obs, caplog.at_level(logging.DEBUG, "maestro.runtime_contract"):
        lines = enforce_runtime_contract(_config(), enforced=True, env_name=None)
    assert lines == ()
    assert caplog.records == []


# ── cmd_preflight ────────────────────────────────────────────────────


def _run_preflight(config: Config) -> tuple[str, int | str | None]:
    buf = io.StringIO()
    code: int | str | None = 0
    with redirect_stdout(buf):
        try:
            cmd_preflight(argparse.Namespace(), config)
        except SystemExit as exc:
            code = exc.code
    return buf.getvalue(), code


def test_preflight_enforced_violation_exits_non_zero(mem_fs: dict[str, str]) -> None:
    load, obs = _patched(_MISSING)
    with load, obs:
        output, code = _run_preflight(_config(runtime_contract_enforced=True))
    assert code == 1
    assert "FAIL: binary npx: not on PATH" in output
    assert "Preflight checks passed" not in output


def test_preflight_unenforced_violation_warns_and_passes(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    load, obs = _patched(_MISSING)
    with load, obs, caplog.at_level(logging.WARNING, "maestro.runtime_contract"):
        output, code = _run_preflight(_config())
    assert code == 0
    assert "runtime_contract: 1 warning(s)" in output
    assert "Preflight checks passed" in output
    assert any("binary npx" in r.getMessage() for r in caplog.records)


def test_preflight_clean_contract_reports_ok(mem_fs: dict[str, str]) -> None:
    load, obs = _patched(_PRESENT)
    with load, obs:
        output, code = _run_preflight(_config(runtime_contract_enforced=True))
    assert code == 0
    assert "runtime_contract: ok" in output


# ── _build_agent ─────────────────────────────────────────────────────


def test_build_agent_enforced_violation_raises(mem_fs: dict[str, str]) -> None:
    load, obs = _patched(_MISSING)
    with load, obs, pytest.raises(SystemExit) as exc:
        _build_agent(_config(runtime_contract_enforced=True))
    assert "binary npx" in str(exc.value)


def test_build_agent_unenforced_violation_warns_and_builds(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    load, obs = _patched(_MISSING)
    with load, obs, caplog.at_level(logging.WARNING, "maestro.runtime_contract"):
        agent = _build_agent(_config())
    assert agent is not None
    assert any("binary npx" in r.getMessage() for r in caplog.records)
