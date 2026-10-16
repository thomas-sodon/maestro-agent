"""The live-state fence: one owner resets the SDK's process-global sinks.

``maestro.sdk`` and ``maestro.providers.registry`` each name every global they
assign under a ``global`` statement in ``_RUNTIME_GLOBALS``; the conftest
autouse fixture ``_own_sdk_runtime`` snapshots and restores that set around
every test (proposal 138). These tests pin the registry against the source, prove
the snapshot/restore round-trip, and reproduce the ``cli.main`` leak that wrote
test rows into the live cost ledger.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from maestro import sdk
from maestro.providers import registry
from tests.conftest import _sdk_runtime_scope


def _global_statement_names(source: str) -> set[str]:
    """Every name declared by a ``global`` statement anywhere in ``source``."""
    return {
        name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Global)
        for name in node.names
    }


def _unregistered_sinks(source: str, registered: tuple[str, ...]) -> set[str]:
    return _global_statement_names(source) - set(registered)


@pytest.mark.parametrize("module", [sdk, registry], ids=["sdk", "registry"])
def test_runtime_globals_registry_matches_global_statements(
    module: ModuleType,
) -> None:
    """Every ``global``-assigned name is registered, and nothing stale is."""
    declared = _global_statement_names(inspect.getsource(module))
    registered = set(module._RUNTIME_GLOBALS)
    unregistered = declared - registered
    stale = registered - declared
    assert unregistered == set(), f"unregistered sink(s): {sorted(unregistered)}"
    assert stale == set(), f"stale registry entries: {sorted(stale)}"
    missing = [name for name in registered if not hasattr(module, name)]
    assert missing == []


def test_guard_flags_a_seeded_unregistered_sink() -> None:
    """Control: a new ``global`` absent from the registry must be caught."""
    source = "_new_sink = None\n\ndef configure(x):\n    global _new_sink\n    _new_sink = x\n"
    assert _unregistered_sinks(source, ()) == {"_new_sink"}
    assert _unregistered_sinks(source, ("_new_sink",)) == set()


_AD_HOC_RESET_HOOK = re.compile(r"^_reset_\w+_for_tests$")


def _ad_hoc_reset_hooks(source: str) -> set[str]:
    """Names of ``_reset_*_for_tests`` functions defined anywhere in ``source``."""
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _AD_HOC_RESET_HOOK.match(node.name)
    }


@pytest.mark.parametrize("module", [sdk, registry], ids=["sdk", "registry"])
def test_no_ad_hoc_reset_hooks(module: ModuleType) -> None:
    """The autouse owner is the sole restorer; no per-module reset hook returns."""
    assert _ad_hoc_reset_hooks(inspect.getsource(module)) == set()


def test_guard_flags_a_seeded_reset_hook() -> None:
    """Control: a seeded hook is caught and an unrelated reset is not."""
    source = "def _reset_x_for_tests():\n    pass\n\ndef reset_x():\n    pass\n"
    assert _ad_hoc_reset_hooks(source) == {"_reset_x_for_tests"}


_REGISTERED_RUNTIME_GLOBALS = frozenset(sdk._RUNTIME_GLOBALS) | frozenset(
    registry._RUNTIME_GLOBALS,
)
_TESTS_ROOT = Path(__file__).resolve().parent


def _direct_runtime_writes(source: str, names: frozenset[str]) -> set[str]:
    """Registered globals assigned by attribute (``<expr>.<name> = ...``).

    ``monkeypatch.setattr``, ``patch.object`` and the ``configure_*`` seams are
    the sanctioned writers; a direct assignment is a hand-rolled restorer that
    can drift from the autouse ``_own_sdk_runtime`` owner.
    """
    targets: list[ast.expr] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor)):
            targets.append(node.target)
    flat: list[ast.expr] = []
    while targets:
        target = targets.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            targets.extend(target.elts)
        elif isinstance(target, ast.Starred):
            targets.append(target.value)
        else:
            flat.append(target)
    return {
        target.attr
        for target in flat
        if isinstance(target, ast.Attribute) and target.attr in names
    }


@pytest.mark.parametrize(
    "path",
    sorted(
        p for p in _TESTS_ROOT.rglob("*.py") if "__pycache__" not in p.parts
    ),
    ids=lambda p: str(p.relative_to(_TESTS_ROOT)),
)
def test_no_direct_runtime_global_writes(path: Path) -> None:
    """No test sets a registered runtime global by direct attribute assignment."""
    source = path.read_text(encoding="utf-8")
    assert _direct_runtime_writes(source, _REGISTERED_RUNTIME_GLOBALS) == set()


def test_guard_flags_a_seeded_direct_runtime_write() -> None:
    """Control: a seeded restorer is caught; setattr and plain reads are not."""
    assert _direct_runtime_writes(
        "sdk_mod._sdk_log_dir = old\n", _REGISTERED_RUNTIME_GLOBALS,
    ) == {"_sdk_log_dir"}
    assert _direct_runtime_writes(
        "registry._phase_provider_names: dict = {}\n", _REGISTERED_RUNTIME_GLOBALS,
    ) == {"_phase_provider_names"}
    assert _direct_runtime_writes(
        "sdk._sdk_log_dir, [*sdk._cost_sample_backend] = a, b\n",
        _REGISTERED_RUNTIME_GLOBALS,
    ) == {"_sdk_log_dir", "_cost_sample_backend"}
    assert _direct_runtime_writes(
        "for sdk._sdk_log_dir in dirs:\n    pass\n", _REGISTERED_RUNTIME_GLOBALS,
    ) == {"_sdk_log_dir"}
    sanctioned = (
        'monkeypatch.setattr(sdk_mod, "_sdk_log_dir", x)\n'
        "x = sdk._sdk_log_dir\n"
    )
    assert _direct_runtime_writes(sanctioned, _REGISTERED_RUNTIME_GLOBALS) == set()


def test_sdk_restore_undoes_configured_sinks() -> None:
    before_backend = sdk._cost_sample_backend
    before_log_dir = sdk._sdk_log_dir
    snapshot = sdk.snapshot_sdk_runtime()
    sdk.configure_cost_tracking(MagicMock())
    sdk.configure_sdk_logging(Path("x"))
    assert sdk._sdk_log_dir == Path("x")
    sdk.restore_sdk_runtime(snapshot)
    assert sdk._cost_sample_backend is before_backend
    assert sdk._sdk_log_dir == before_log_dir


def test_sdk_restore_undoes_in_place_container_mutation() -> None:
    before = set(sdk._pricing_warned)
    snapshot = sdk.snapshot_sdk_runtime()
    sdk._pricing_warned.add("fixture-model")
    sdk.restore_sdk_runtime(snapshot)
    assert sdk._pricing_warned == before


def test_provider_restore_undoes_selection() -> None:
    before_name = registry.current_provider_name()
    before_phases = dict(registry._phase_provider_names)
    snapshot = registry.snapshot_provider_runtime()
    registry.configure_provider("cli_paced")
    registry.configure_phase_providers({"introspect": "cli_paced"})
    assert registry.current_provider_name() == "cli_paced"
    registry.restore_provider_runtime(snapshot)
    assert registry.current_provider_name() == before_name
    assert registry._phase_provider_names == before_phases


def test_scope_contains_workspace_counterfactual_leak(
    mem_fs: dict[str, str],
) -> None:
    """The exact sequence that leaked into the live ledger stays in scope."""
    from maestro.cli import main

    before_backend = sdk._cost_sample_backend
    before_log_dir = sdk._sdk_log_dir
    with _sdk_runtime_scope():
        with patch("sys.argv", ["maestro", "workspace-counterfactual"]), \
                patch("maestro.cli.cmd_workspace_counterfactual"):
            main()
        assert sdk._cost_sample_backend is not None
        assert sdk._sdk_log_dir is not None
    assert sdk._cost_sample_backend is before_backend
    assert sdk._sdk_log_dir == before_log_dir


def test_scope_contains_run_path_cost_backend_leak(
    mem_fs: dict[str, str],
) -> None:
    """The ``run`` path leaves only ``configure_cost_tracking`` unpatched."""
    from maestro.cli import main

    before_backend = sdk._cost_sample_backend
    with _sdk_runtime_scope():
        with patch("sys.argv", ["maestro", "run", "test goal"]), \
                patch("maestro.cli.cmd_run"), \
                patch("maestro.sdk.configure_sdk_logging"), \
                patch("maestro.sdk.configure_sdk_model"), \
                patch(
                    "logging.handlers.RotatingFileHandler",
                    return_value=MagicMock(),
                ):
            main()
        assert sdk._cost_sample_backend is not None
    assert sdk._cost_sample_backend is before_backend


def test_scope_removes_and_closes_added_logger_handlers() -> None:
    maestro_logger = logging.getLogger("maestro")
    level_before = maestro_logger.level
    added = logging.NullHandler()
    with patch.object(added, "close") as mock_close, _sdk_runtime_scope():
        maestro_logger.addHandler(added)
        maestro_logger.setLevel(logging.CRITICAL)
    assert added not in maestro_logger.handlers
    mock_close.assert_called_once()
    assert maestro_logger.level == level_before


def test_scope_reattaches_removed_logger_handlers() -> None:
    maestro_logger = logging.getLogger("maestro")
    existing = logging.NullHandler()
    maestro_logger.addHandler(existing)
    try:
        with _sdk_runtime_scope():
            maestro_logger.removeHandler(existing)
        assert existing in maestro_logger.handlers
    finally:
        maestro_logger.removeHandler(existing)


def test_sdk_restore_keeps_non_container_sinks_by_identity() -> None:
    """A pre-set backend and semaphore come back as the same objects."""
    backend = MagicMock()
    sdk.configure_cost_tracking(backend)
    semaphore = sdk._concurrency_semaphore
    snapshot = sdk.snapshot_sdk_runtime()
    sdk.configure_cost_tracking(MagicMock())
    sdk.restore_sdk_runtime(snapshot)
    assert sdk._cost_sample_backend is backend
    assert sdk._concurrency_semaphore is semaphore


def test_provider_snapshot_copies_phase_map() -> None:
    snapshot = registry.snapshot_provider_runtime()
    assert snapshot["_phase_provider_names"] is not registry._phase_provider_names
    assert snapshot["_active_provider_name"] is registry._active_provider_name
