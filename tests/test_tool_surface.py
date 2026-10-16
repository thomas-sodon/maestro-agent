"""Tests for the tool-surface drift sensor (proposal 142, step 2)."""

from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

from maestro import call_policy, tool_surface
from maestro import executor as executor_mod
from maestro import sdk as sdk_mod
from maestro.cli import cmd_tool_surface_audit
from maestro.config import Config
from maestro.providers.cli_paced import _tool_policy_flags
from maestro.sdk import QueryOptions, _build_sdk_options
from maestro.tool_gating import make_task_tool_hooks
from maestro.tool_surface import (
    REFLECTION_READ_ONLY,
    REPO_MUTATING,
    RUNTIME_GROUNDED_SURFACES,
    TASK_TOOL_BUILTINS,
    TOOL_FREE,
    TREE_READ_ONLY,
    ToolSurface,
    declared_builtins,
    declared_mcp_servers,
    deny_ungated_shell,
    effective_surface,
    measure_tool_surface_drift,
    render_report,
    resolve_call_options,
    ungated_shell_builtins,
)
from maestro.transcript import TranscriptRecord, TranscriptTurn
from tests.conftest import capture_stdout

_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"


def _call(name: str) -> TranscriptTurn:
    return TranscriptTurn("tool_call", "assistant", "t", f"{name} input={{}}")


def _result(content: str) -> TranscriptTurn:
    return TranscriptTurn("tool_result", "tool", "t", content)


def _rec(
    caller: str,
    *turns: TranscriptTurn,
    advertised: list[str] | None = None,
    servers: list[str] | None = None,
) -> TranscriptRecord:
    return TranscriptRecord(
        caller=caller,
        session_id="abc123",
        started_at="2026-09-30T00:00:00Z",
        tools_advertised=advertised,
        mcp_servers_advertised=servers,
        turns=list(turns),
    )


class TestDeclaredBuiltins:
    def test_task_tool_caller(self) -> None:
        assert declared_builtins("implement_plan") == TASK_TOOL_BUILTINS

    def test_tool_free_registry_caller(self) -> None:
        assert declared_builtins("propose_plan") == frozenset()

    def test_formerly_undeclared_registry_caller_is_tool_free(self) -> None:
        assert declared_builtins("respond_to_messages") == frozenset()

    def test_grounded_surface_caller_declares_ungrounded(self) -> None:
        assert declared_builtins("introspect") == frozenset()

    def test_unregistered_caller(self) -> None:
        assert declared_builtins("not_a_caller") is None


class TestEffectiveSurface:
    def test_reflection_ungrounded_is_tool_free(self) -> None:
        assert effective_surface("introspect", grounded=False) is TOOL_FREE

    def test_reflection_grounded_is_itself(self) -> None:
        assert (
            effective_surface("introspect", grounded=True)
            is REFLECTION_READ_ONLY
        )

    @pytest.mark.parametrize("grounded", [False, True])
    def test_repo_mutating_is_identity(self, grounded: bool) -> None:
        assert (
            effective_surface("auto_fix", grounded=grounded) is REPO_MUTATING
        )

    @pytest.mark.parametrize("grounded", [False, True])
    def test_tool_free_is_identity(self, grounded: bool) -> None:
        assert (
            effective_surface("rejection_classifier", grounded=grounded)
            is TOOL_FREE
        )

    def test_unregistered_raises(self) -> None:
        with pytest.raises(KeyError):
            effective_surface("not_a_caller", grounded=False)

    def test_runtime_grounded_set(self) -> None:
        assert (
            frozenset({REFLECTION_READ_ONLY, TREE_READ_ONLY})
            == RUNTIME_GROUNDED_SURFACES
        )

    def test_tree_read_only_is_only_the_tree_server(self) -> None:
        from maestro.tree_retrieval import TREE_SERVER_NAME

        assert TREE_READ_ONLY.mcp_servers == frozenset({TREE_SERVER_NAME})
        assert TREE_READ_ONLY.builtins == frozenset()
        assert TREE_READ_ONLY.external_mcp is False

    @pytest.mark.parametrize("grounded", [False, True])
    def test_propose_plan_resolves_by_grounding(self, grounded: bool) -> None:
        expected = TREE_READ_ONLY if grounded else TOOL_FREE
        assert effective_surface("propose_plan", grounded=grounded) is expected

    @pytest.mark.parametrize("caller", ["propose_plan"])
    def test_canary_caller_admits_tree_server_only(self, caller: str) -> None:
        from maestro.config import Config
        from maestro.sdk import QueryOptions
        from maestro.tree_retrieval import TREE_SERVER_NAME

        cfg = Config(context7_mcp_enabled=False)
        grounded = resolve_call_options(
            QueryOptions(caller=caller, mcp_servers={TREE_SERVER_NAME: {}}),
            cfg,
        )
        assert grounded.tools == []
        with pytest.raises(ValueError, match="does not admit MCP server"):
            resolve_call_options(
                QueryOptions(caller=caller, mcp_servers={"serena": {}}), cfg,
            )


class TestMeasure:
    def test_monitor_off_surface_for_task_caller(self) -> None:
        report = measure_tool_surface_drift([
            _rec("implement_plan", _call("Monitor"), _call("Read")),
        ])
        (drift,) = report.callers
        assert drift.off_surface_executed == {"Monitor": 1}
        assert drift.sidecars_with_off_surface == 1
        assert report.off_surface_total == 1

    def test_refused_call_is_not_executed(self) -> None:
        report = measure_tool_surface_drift([
            _rec(
                "propose_plan",
                _call("Bash"),
                _result("ERROR <tool_use_error>No such tool available: Bash"),
            ),
        ])
        (drift,) = report.callers
        assert drift.off_surface_executed == {}
        assert drift.refusals == {"Bash": 1}
        assert drift.sidecars_with_off_surface == 0
        assert report.refusals_total == 1

    def test_non_error_result_quoting_refusal_is_not_a_refusal(self) -> None:
        report = measure_tool_surface_drift([
            _rec(
                "propose_plan",
                _call("Read"),
                _result("docs say: No such tool available: Bash"),
            ),
        ])
        (drift,) = report.callers
        assert drift.refusals == {}
        assert drift.off_surface_executed == {"Read": 1}

    def test_undeclared_caller_counts_default_toolbelt(self) -> None:
        report = measure_tool_surface_drift([
            _rec("not_a_caller", _call("Bash"), _result("ok")),
        ])
        (drift,) = report.callers
        assert drift.declared is None
        assert drift.off_surface_executed == {"Bash": 1}

    def test_formerly_undeclared_caller_counts_unchanged(self) -> None:
        report = measure_tool_surface_drift([
            _rec("respond_to_messages", _call("Bash"), _result("ok")),
        ])
        (drift,) = report.callers
        assert drift.declared == frozenset()
        assert drift.off_surface_executed == {"Bash": 1}

    def test_mcp_and_structured_output_excluded(self) -> None:
        report = measure_tool_surface_drift([
            _rec(
                "propose_plan",
                _call("mcp__serena__find_symbol"),
                _call("StructuredOutput"),
                _result("ERROR No such tool available: mcp__x__y"),
                advertised=["mcp__serena__find_symbol", "StructuredOutput"],
            ),
        ])
        (drift,) = report.callers
        assert drift.has_drift is False
        assert report.has_drift is False

    def test_unknown_caller_skipped(self) -> None:
        report = measure_tool_surface_drift([_rec("unknown", _call("Bash"))])
        assert report.callers == ()
        assert report.skipped_unknown == 1
        assert report.sidecars == 0

    def test_advertised_unknown_and_minus_declared(self) -> None:
        report = measure_tool_surface_drift([
            _rec("implement_plan"),
            _rec("implement_plan", advertised=["Read", "Monitor"]),
        ])
        (drift,) = report.callers
        assert drift.sidecars == 2
        assert drift.advertised_unknown == 1
        assert drift.advertised_minus_declared == frozenset({"Monitor"})

    def test_malformed_tool_call_ignored(self) -> None:
        report = measure_tool_surface_drift([
            _rec(
                "respond_to_messages",
                TranscriptTurn("tool_call", "assistant", "t", "garbage"),
                TranscriptTurn("text", "assistant", "t", "Bash input={}"),
            ),
        ])
        (drift,) = report.callers
        assert drift.off_surface_executed == {}

    def test_seeded_fault_admitting_monitor_diverges(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        records = [_rec("implement_plan", _call("Monitor"))]
        baseline = measure_tool_surface_drift(records).off_surface_total
        monkeypatch.setattr(
            tool_surface, "TASK_TOOL_BUILTINS", TASK_TOOL_BUILTINS | {"Monitor"},
        )
        seeded = measure_tool_surface_drift(records).off_surface_total
        assert baseline == 1
        assert seeded == 0


class TestAdvertisedServers:
    def test_tool_free_caller_reports_serena(self) -> None:
        report = measure_tool_surface_drift([
            _rec("rejection_classifier", servers=["serena"]),
        ])
        (drift,) = report.callers
        assert drift.mcp_servers_advertised_undeclared == frozenset({"serena"})
        assert drift.has_drift is True

    def test_external_surface_admits_serena(self) -> None:
        report = measure_tool_surface_drift([
            _rec("implement_plan", servers=["serena", "journal"]),
        ])
        (drift,) = report.callers
        assert drift.mcp_servers_advertised_undeclared == frozenset()

    def test_grounded_introspect_admits_journal(self) -> None:
        report = measure_tool_surface_drift([
            _rec("introspect", servers=["journal"]),
        ])
        (drift,) = report.callers
        assert drift.mcp_servers_advertised_undeclared == frozenset()
        assert drift.has_drift is False

    def test_none_field_counts_unknown(self) -> None:
        report = measure_tool_surface_drift([
            _rec("introspect"), _rec("introspect", servers=[]),
        ])
        (drift,) = report.callers
        assert drift.mcp_servers_advertised_unknown == 1
        assert drift.mcp_servers_advertised_undeclared == frozenset()
        assert drift.has_drift is False

    def test_external_mcp_surface_admits_any_server_when_unconfigured(
        self,
    ) -> None:
        report = measure_tool_surface_drift([
            _rec("implement_plan", servers=["serena", "stray"]),
        ])
        (drift,) = report.callers
        assert drift.mcp_servers_advertised_undeclared == frozenset()

    def test_external_mcp_surface_reports_unconfigured_server(self) -> None:
        report = measure_tool_surface_drift(
            [_rec("implement_plan", servers=["serena", "stray"])],
            external_servers=frozenset({"serena"}),
        )
        (drift,) = report.callers
        assert drift.mcp_servers_advertised_undeclared == frozenset({"stray"})

    def test_unregistered_caller_reports_every_server(self) -> None:
        report = measure_tool_surface_drift([
            _rec("not_a_caller", servers=["journal", "tree"]),
        ])
        (drift,) = report.callers
        assert drift.mcp_servers_advertised_undeclared == frozenset(
            {"journal", "tree"},
        )

    def test_declared_mcp_servers(self) -> None:
        assert declared_mcp_servers("not_a_caller") is None
        assert declared_mcp_servers("rejection_classifier") == frozenset()
        assert declared_mcp_servers("introspect") == (
            REFLECTION_READ_ONLY.mcp_servers
        )

    def test_payload_and_render(self) -> None:
        report = measure_tool_surface_drift([
            _rec("rejection_classifier", servers=["serena"]), _rec("rejection_classifier"),
        ])
        (row,) = report.report_payload()["callers"]  # type: ignore[misc]
        assert row["mcp_servers_advertised_undeclared"] == ["serena"]
        assert row["mcp_servers_advertised_unknown"] == 1
        text = render_report(report)
        assert (
            "advertised-undeclared servers: serena (1 advertised-unknown)"
            in text
        )

    def test_seeded_fault_emptying_reflection_servers_reports_journal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        records = [_rec("introspect", servers=["journal"])]
        baseline = measure_tool_surface_drift(records)
        patched = dict(call_policy.CALL_POLICIES)
        patched["introspect"] = replace(
            patched["introspect"],
            surface=ToolSurface(frozenset(), frozenset(), external_mcp=False),
        )
        monkeypatch.setattr(
            call_policy, "CALL_POLICIES", MappingProxyType(patched),
        )
        seeded = measure_tool_surface_drift(records)
        assert baseline.callers[0].mcp_servers_advertised_undeclared == frozenset()
        assert seeded.callers[0].mcp_servers_advertised_undeclared == frozenset(
            {"journal"},
        )


class TestRender:
    def test_no_drift_one_line(self) -> None:
        report = measure_tool_surface_drift([_rec("propose_plan")])
        text = render_report(report)
        assert text == "No tool-surface drift (1 sidecars across 1 callers)."

    def test_drift_rendering(self) -> None:
        report = measure_tool_surface_drift([
            _rec("implement_plan", _call("Monitor"), advertised=["Monitor"]),
            _rec("propose_plan"),
            _rec(
                "evolution_session",
                _call("Bash"),
                _result("ERROR No such tool available: Bash"),
            ),
            _rec("not_a_caller", _call("Bash")),
        ])
        text = render_report(report)
        assert "Off-surface calls:   2" in text
        assert "off-surface executed: Monitor=1 (in 1 sidecars)" in text
        assert "refused:              Bash=1" in text
        assert "none (tool-free)" in text
        assert "default toolbelt (undeclared)" in text
        assert "advertised-undeclared: Monitor" in text
        assert "propose_plan" not in text


def _task_tool_list_literals() -> list[tuple[str, int, frozenset[str]]]:
    found: list[tuple[str, int, frozenset[str]]] = []
    for path in sorted(_MAESTRO_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            names = frozenset(
                elt.value for elt in node.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            )
            if "Bash" in names and names & {"Read", "Write"}:
                found.append((path.name, node.lineno, names))
    return found


def test_task_tool_literals_match_sensor_baseline() -> None:
    literals = _task_tool_list_literals()
    mismatched = [
        (name, line) for name, line, names in literals
        if names != TASK_TOOL_BUILTINS
    ]
    assert literals != []
    assert mismatched == []


def _task_tool_callers() -> list[tuple[str, int, str | None]]:
    """Caller tag of every ``QueryOptions`` carrying a six-name tool literal.

    The tag is the ``caller=`` keyword, else the first positional string of an
    enclosing ``single_use_call(...)`` (which stamps the caller itself).
    """
    found: list[tuple[str, int, str | None]] = []
    for path in sorted(_MAESTRO_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wrappers: dict[int, str] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "single_use_call"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                for kw in node.keywords:
                    if kw.arg == "options":
                        wrappers[id(kw.value)] = str(node.args[0].value)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "QueryOptions"
            ):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            tools = kwargs.get("allowed_tools")
            if not isinstance(tools, ast.List) or not any(
                isinstance(e, ast.Constant) and e.value == "Bash"
                for e in tools.elts
            ):
                continue
            caller = kwargs.get("caller")
            tag = (
                str(caller.value) if isinstance(caller, ast.Constant)
                else wrappers.get(id(node))
            )
            found.append((path.name, node.lineno, tag))
    return found


def test_task_tool_literal_callers_are_declared() -> None:
    sites = _task_tool_callers()
    undeclared = [
        (name, line, tag) for name, line, tag in sites
        if tag not in tool_surface.DECLARED_TASK_TOOL_CALLERS
    ]
    assert sites != []
    assert undeclared == []


def _args(last: int | None = None, json_out: bool = False) -> argparse.Namespace:
    return argparse.Namespace(last=last, json=json_out)


def _config() -> MagicMock:
    config = MagicMock(spec=Config)
    config.sdk_log_dir = Path("/fake/logs/sdk")
    config.mcp_servers_config.return_value = None
    return config


class TestCli:
    def test_forwards_configured_external_servers(self) -> None:
        config = _config()
        config.mcp_servers_config.return_value = {"serena": {}}
        records = [
            ("2026-09-30", _rec("implement_plan", servers=["serena", "stray"])),
        ]
        with patch(
            "maestro.logs.iter_transcript_records", return_value=records,
        ):
            payload = json.loads(
                capture_stdout(
                    cmd_tool_surface_audit, _args(json_out=True), config,
                ),
            )
        (row,) = payload["callers"]
        assert row["mcp_servers_advertised_undeclared"] == ["stray"]

    def test_json_shape(self) -> None:
        records = [
            ("2026-09-30", _rec("implement_plan", _call("Monitor"))),
            ("2026-09-30", _rec("propose_plan", advertised=["Read"])),
        ]
        with patch(
            "maestro.logs.iter_transcript_records", return_value=iter(records),
        ) as it:
            out = capture_stdout(
                cmd_tool_surface_audit, _args(last=3, json_out=True), _config(),
            )
        it.assert_called_once_with(Path("/fake/logs/sdk"), 3)
        payload = json.loads(out)
        assert payload["off_surface_total"] == 1
        assert payload["callers"][0]["caller"] == "implement_plan"
        assert payload["callers"][0]["off_surface_executed"] == {"Monitor": 1}
        assert payload["callers"][1]["declared"] == []
        assert payload["callers"][1]["advertised_minus_declared"] == ["Read"]
        assert payload["callers"][1]["advertised_unknown"] == 0

    def test_json_undeclared_caller(self) -> None:
        records = [("d", _rec("not_a_caller"))]
        with patch(
            "maestro.logs.iter_transcript_records", return_value=iter(records),
        ):
            out = capture_stdout(
                cmd_tool_surface_audit, _args(json_out=True), _config(),
            )
        payload = json.loads(out)
        assert payload["callers"][0]["declared"] is None

    def test_text_rendering(self) -> None:
        records = [("d", _rec("implement_plan", _call("Monitor")))]
        with patch(
            "maestro.logs.iter_transcript_records", return_value=iter(records),
        ):
            out = capture_stdout(cmd_tool_surface_audit, _args(), _config())
        assert "Tool-Surface Drift" in out

    def test_empty_log_no_drift(self) -> None:
        with patch(
            "maestro.logs.iter_transcript_records", return_value=iter([]),
        ) as it:
            out = capture_stdout(cmd_tool_surface_audit, _args(last=0), _config())
        it.assert_called_once_with(Path("/fake/logs/sdk"), None)
        assert out.strip() == (
            "No tool-surface drift (0 sidecars across 0 callers)."
        )

    def test_nonexistent_sdk_log_dir(self, mem_fs: dict[str, str]) -> None:
        out = capture_stdout(cmd_tool_surface_audit, _args(), _config())
        assert "No tool-surface drift (0 sidecars" in out


def _surface_config(serena: bool = False) -> Config:
    return Config(
        context7_mcp_enabled=False,
        serena_mcp_enabled=serena,
    )


def _shell_gate_violations(
    surfaces: Mapping[str, tool_surface.ToolSurface],
    shell: frozenset[str],
    gated: frozenset[str],
) -> list[str]:
    """Names of surfaces granting a shell-capable built-in the gate misses."""
    return [
        name for name, s in surfaces.items()
        if not (s.builtins & shell) <= gated
    ]


def _dict_literal_keys(tree: ast.AST, func_name: str) -> set[str]:
    """String keys of *func_name*'s dict literals, minus the ``registry`` wrapper."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict) and "registry" not in {
                    k.value for k in sub.keys if isinstance(k, ast.Constant)
                }:
                    keys |= {
                        k.value for k in sub.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
    return keys


class TestSurfaceVocabulary:
    def test_shell_capable_builtins_are_gate_matched(self) -> None:
        violations = _shell_gate_violations(
            tool_surface.NAMED_SURFACES,
            tool_surface.SHELL_CAPABLE_BUILTINS,
            tool_surface.GATE_MATCHED_TOOLS,
        )
        assert violations == []

    def test_introspection_servers_match_cli_registry(self) -> None:
        import maestro.cli as cli_mod

        tree = ast.parse(Path(cli_mod.__file__).read_text(encoding="utf-8"))
        registry = _dict_literal_keys(tree, "_build_agent_tools")
        assert registry == tool_surface._INTROSPECTION_SERVERS

    def test_tree_server_not_in_cli_registry(self) -> None:
        import maestro.cli as cli_mod
        from maestro.tree_retrieval import TREE_SERVER_NAME

        tree = ast.parse(Path(cli_mod.__file__).read_text(encoding="utf-8"))
        registry = _dict_literal_keys(tree, "_build_agent_tools")
        assert TREE_SERVER_NAME not in registry

    def test_repo_mutating_extra_servers_match_owners(self) -> None:
        from maestro import email_tools, telegram_tools

        tree = ast.parse(Path(executor_mod.__file__).read_text(encoding="utf-8"))
        messaging_keys = _dict_literal_keys(tree, "_build_messaging_options")
        expected = tool_surface._INTROSPECTION_SERVERS | messaging_keys | {
            email_tools.SERVER_KEY, telegram_tools.SERVER_KEY,
        }
        assert tool_surface.REPO_MUTATING.mcp_servers == expected

    def test_no_surface_grants_agent(self) -> None:
        for surface in tool_surface.NAMED_SURFACES.values():
            assert "Agent" not in surface.builtins

    def test_repo_mutating_covers_task_tool_baseline(self) -> None:
        assert tool_surface.REPO_MUTATING.builtins >= TASK_TOOL_BUILTINS

    def test_tool_free_resolves_empty(self) -> None:
        resolved = tool_surface.resolve_tool_options(
            tool_surface.TOOL_FREE, _surface_config(),
        )
        assert resolved.tools == []
        assert resolved.mcp_server_names == frozenset()
        assert resolved.disallowed_tools is None

    def test_repo_mutating_serena_off_keeps_default_deny(self) -> None:
        resolved = tool_surface.resolve_tool_options(
            tool_surface.REPO_MUTATING, _surface_config(),
        )
        assert resolved.disallowed_tools is None
        assert resolved.tools == sorted(tool_surface.REPO_MUTATING.builtins)
        assert resolved.mcp_server_names == tool_surface.REPO_MUTATING.mcp_servers

    def test_repo_mutating_serena_on_carries_deny_list(self) -> None:
        cfg = _surface_config(serena=True)
        resolved = tool_surface.resolve_tool_options(
            tool_surface.REPO_MUTATING, cfg,
        )
        assert resolved.disallowed_tools == cfg.external_mcp_disallowed_tools()
        assert "serena" in resolved.mcp_server_names

    def test_reflection_ignores_external_servers(self) -> None:
        resolved = tool_surface.resolve_tool_options(
            tool_surface.REFLECTION_READ_ONLY, _surface_config(serena=True),
        )
        assert "serena" not in resolved.mcp_server_names
        assert resolved.disallowed_tools is None

    def test_admits_builtin_names(self) -> None:
        cfg = _surface_config()
        assert tool_surface.admits_tool_name(tool_surface.REPO_MUTATING, "Bash", cfg)
        assert not tool_surface.admits_tool_name(
            tool_surface.REPO_MUTATING, "Monitor", cfg,
        )
        assert not tool_surface.admits_tool_name(tool_surface.TOOL_FREE, "Read", cfg)

    def test_admits_mcp_names_by_server(self) -> None:
        cfg = _surface_config()
        assert tool_surface.admits_tool_name(
            tool_surface.REFLECTION_READ_ONLY, "mcp__journal__read_journal", cfg,
        )
        assert not tool_surface.admits_tool_name(
            tool_surface.REFLECTION_READ_ONLY, "mcp__messaging__send", cfg,
        )

    def test_denied_serena_tool_refused(self) -> None:
        cfg = _surface_config(serena=True)
        denied = next(
            n for n in cfg.external_mcp_disallowed_tools() or []
            if n.startswith("mcp__serena__")
        )
        assert not tool_surface.admits_tool_name(
            tool_surface.REPO_MUTATING, denied, cfg,
        )
        assert tool_surface.admits_tool_name(
            tool_surface.REPO_MUTATING, "mcp__serena__find_symbol", cfg,
        )

    def test_structured_output_always_admitted(self) -> None:
        cfg = _surface_config()
        for surface in tool_surface.NAMED_SURFACES.values():
            assert tool_surface.admits_tool_name(surface, "StructuredOutput", cfg)

    def test_seeded_monitor_fault_breaks_invariant(self) -> None:
        src = Path(tool_surface.__file__).read_text(encoding="utf-8")
        needle = 'TASK_TOOL_BUILTINS | {"TaskOutput", "TaskStop"}'
        assert needle in src
        mutated = src.replace(
            needle, 'TASK_TOOL_BUILTINS | {"TaskOutput", "TaskStop", "Monitor"}',
        )
        namespace: dict[str, object] = {"__name__": tool_surface.__name__}
        exec(compile(mutated, "<mutated_tool_surface>", "exec"), namespace)
        surfaces = namespace["NAMED_SURFACES"]
        shell = namespace["SHELL_CAPABLE_BUILTINS"]
        gated = namespace["GATE_MATCHED_TOOLS"]
        assert isinstance(surfaces, Mapping)
        assert isinstance(shell, frozenset)
        assert isinstance(gated, frozenset)
        violations = _shell_gate_violations(surfaces, shell, gated)
        assert violations == ["repo_mutating"]
        assert tool_surface.REPO_MUTATING.builtins.isdisjoint({"Monitor"})


def _grounded_with_builtins(
    grounded: frozenset[tool_surface.ToolSurface],
) -> list[tool_surface.ToolSurface]:
    return [surface for surface in grounded if surface.builtins]


def test_runtime_grounded_surfaces_have_no_builtins() -> None:
    """Grounding can never change a call's ``tools`` (proposal 142)."""
    assert _grounded_with_builtins(tool_surface.RUNTIME_GROUNDED_SURFACES) == []


def test_runtime_grounded_builtins_fault_is_detected() -> None:
    seeded = tool_surface.RUNTIME_GROUNDED_SURFACES | {tool_surface.REPO_MUTATING}
    assert _grounded_with_builtins(seeded) == [tool_surface.REPO_MUTATING]


def test_derived_phase_sets_golden() -> None:
    """Proposal 142 step 6: the derived sets equal the retired hand-kept ones."""
    assert tool_surface.action_gated_phases() == {"implementation", "run_task"}
    assert tool_surface.mcp_attaching_phases(grounded=False) == {
        "implementation", "run_task",
    }
    assert tool_surface.mcp_attaching_phases(grounded=True) == {
        "implementation", "run_task", "contemplation", "evolution",
    }
    assert tool_surface.external_mcp_phases(grounded=False) == {"implementation", "run_task"}
    assert tool_surface.external_mcp_phases(grounded=True) == {"implementation", "run_task"}
    assert tool_surface.gate_hook_matcher() == "Bash"
    assert tool_surface.is_gate_matched("Bash") is True
    assert tool_surface.is_gate_matched("Monitor") is False


def test_seeded_registry_moves_action_gated_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = dict(call_policy.CALL_POLICIES)
    patched["contemplation_session"] = replace(
        patched["contemplation_session"], surface=tool_surface.REPO_MUTATING,
    )
    monkeypatch.setattr(call_policy, "CALL_POLICIES", MappingProxyType(patched))
    assert "contemplation" in tool_surface.action_gated_phases()


def _call_site_disagreements(tree: ast.AST) -> list[tuple[int, str, str]]:
    """``(line, caller, name)`` for every literal tool name a literal caller's surface refuses.

    Walks every ``QueryOptions(...)`` with a string-literal ``caller=``; an
    unregistered caller is reported with an empty name. Each string-literal
    element of its ``tools=`` / ``allowed_tools=`` lists is checked against the
    caller's grounded surface under a default :class:`Config`. It pins only
    literal tool-name agreement (proposal 142): ``mcp_servers`` admission and
    session-turn agreement are not checked here.
    """
    config = Config()
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "QueryOptions"
        ):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        caller = kwargs.get("caller")
        if not (isinstance(caller, ast.Constant) and isinstance(caller.value, str)):
            continue
        tag = caller.value
        if tag not in call_policy.CALL_POLICIES:
            found.append((node.lineno, tag, ""))
            continue
        surface = effective_surface(tag, grounded=True)
        for key in ("tools", "allowed_tools"):
            names = kwargs.get(key)
            if not isinstance(names, ast.List):
                continue
            for elt in names.elts:
                if (
                    isinstance(elt, ast.Constant)
                    and isinstance(elt.value, str)
                    and not tool_surface.admits_tool_name(surface, elt.value, config)
                ):
                    found.append((node.lineno, tag, elt.value))
    return found


def test_literal_call_sites_agree_with_caller_surface() -> None:
    """Every literal ``QueryOptions`` call site is admitted by its caller's surface."""
    disagreements: list[tuple[str, int, str, str]] = []
    literal_sites = 0
    for path in sorted(_MAESTRO_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "QueryOptions(" not in source:
            continue
        tree = ast.parse(source)
        literal_sites += sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "QueryOptions"
            and any(
                kw.arg == "caller" and isinstance(kw.value, ast.Constant)
                for kw in node.keywords
            )
        )
        disagreements.extend(
            (path.name, line, tag, name)
            for line, tag, name in _call_site_disagreements(tree)
        )
    assert literal_sites > 0
    assert disagreements == []


def test_call_site_guard_flags_seeded_off_surface_name() -> None:
    seeded = ast.parse('QueryOptions(caller="propose_plan", allowed_tools=["Bash"])')
    assert _call_site_disagreements(seeded) == [(1, "propose_plan", "Bash")]


def test_call_site_guard_flags_seeded_unregistered_caller() -> None:
    seeded = ast.parse('QueryOptions(caller="no_such_caller", tools=[])')
    assert _call_site_disagreements(seeded) == [(1, "no_such_caller", "")]


def test_call_site_guard_admits_surface_names_and_skips_dynamic() -> None:
    seeded = ast.parse(
        'QueryOptions(caller="execute_step", tools=["Bash", name], '
        'allowed_tools=dynamic)\n'
        'QueryOptions(caller=tag, allowed_tools=["Bash"])\n'
        'other(caller="propose_plan", allowed_tools=["Bash"])'
    )
    assert _call_site_disagreements(seeded) == []


class TestUngatedShellDenial:
    """Issue #4381: both seams deny every shell built-in the gate cannot see."""

    @staticmethod
    def _implement_options(disallowed: list[str] | None) -> QueryOptions:
        return QueryOptions(
            allowed_tools=sorted(TASK_TOOL_BUILTINS),
            hooks=make_task_tool_hooks(frozenset({"maestro/evolve/x"})),
            disallowed_tools=disallowed,
            caller="implement_plan",
        )

    def test_owner_is_shell_capable_minus_gate_matched(self) -> None:
        assert ungated_shell_builtins() == frozenset({"Monitor"})

    def test_gated_session_denies_ungated_shell_builtins(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sdk_mod, "_enforced_tool_surface_config", None)
        sdk_opts, _ = _build_sdk_options(self._implement_options(None), None)
        assert "Monitor" in sdk_opts["disallowed_tools"]
        assert "Agent" in sdk_opts["disallowed_tools"]

    def test_serena_deny_list_survives_the_union(self) -> None:
        serena = Config(serena_mcp_enabled=True).external_mcp_disallowed_tools()
        assert serena
        sdk_opts, _ = _build_sdk_options(self._implement_options(serena), None)
        assert sdk_opts["disallowed_tools"] == [*serena, "Monitor"]

    def test_no_options_path_denies_ungated_shell(self) -> None:
        sdk_opts, _ = _build_sdk_options(None, None)
        assert sdk_opts["disallowed_tools"] == ["Agent", "Monitor"]

    def test_union_drops_duplicates(self) -> None:
        assert deny_ungated_shell(["Monitor", "Agent"]) == ["Monitor", "Agent"]

    def test_cli_paced_emits_denial_with_and_without_caller_list(self) -> None:
        unset = _tool_policy_flags(self._implement_options(None))
        assert unset[unset.index("--disallowedTools") + 1:] == ["Agent", "Monitor"]
        serena = Config(serena_mcp_enabled=True).external_mcp_disallowed_tools()
        assert serena
        given = _tool_policy_flags(self._implement_options(serena))
        assert given[given.index("--disallowedTools") + 1:] == [*serena, "Monitor"]

    def test_seeded_gate_coverage_lifts_the_denial(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            tool_surface, "GATE_MATCHED_TOOLS", frozenset({"Bash", "Monitor"}),
        )
        sdk_opts, _ = _build_sdk_options(self._implement_options(None), None)
        assert sdk_opts["disallowed_tools"] == ["Agent"]
        assert _tool_policy_flags(None) == ["--disallowedTools", "Agent"]

    @pytest.mark.parametrize("caller", sorted(call_policy.CALL_POLICIES))
    def test_every_caller_denies_ungated_shell_flag_on_and_off(
        self, caller: str,
    ) -> None:
        cfg = Config(context7_mcp_enabled=False)
        off = QueryOptions(caller=caller, hooks={"PreToolUse": [object()]})
        on = resolve_call_options(off, cfg)
        for options in (off, on):
            sdk_opts, _ = _build_sdk_options(options, None)
            assert ungated_shell_builtins() <= set(sdk_opts["disallowed_tools"])
            flags = _tool_policy_flags(options)
            denied = set(flags[flags.index("--disallowedTools") + 1:])
            assert ungated_shell_builtins() <= denied


# ── Proposal 146: the web has one owner ─────────────────────────────────────

_FILE_TOOLS = frozenset({"Read", "Write", "Edit", "Glob", "Grep", "TaskOutput", "TaskStop"})


def _overreaching_web_surfaces(
    surfaces: Mapping[str, tool_surface.ToolSurface],
) -> list[str]:
    """Web-reaching surfaces that also carry a shell, a file tool or any MCP."""
    return sorted(
        name
        for name, surface in surfaces.items()
        if surface.builtins & tool_surface.WEB_BUILTINS
        and (
            surface.builtins & (tool_surface.SHELL_CAPABLE_BUILTINS | _FILE_TOOLS)
            or surface.mcp_servers
            or surface.external_mcp
        )
    )


def _research_surface_callers(
    policies: Mapping[str, call_policy.CallPolicy],
) -> list[str]:
    return sorted(
        caller
        for caller, policy in policies.items()
        if policy.surface.builtins & tool_surface.WEB_BUILTINS
    )



