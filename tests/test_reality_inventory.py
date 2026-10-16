"""Tests for :mod:`maestro.reality_inventory` (proposal 136)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro.audit_checks import RULE_AUTHORITY, AuditFinding
from maestro.config import Config
from maestro.memory import AuditResult, Memory, RejectionRecord, SensorReading
from maestro.reality_inventory import (
    MAX_CLEARANCE_NAMES,
    MAX_OFFENDING_CHARS,
    MAX_PROBLEM_SUMMARY_CHARS,
    MAX_PROBLEMS_PER_SOURCE,
    MAX_RENDERED_CAPABILITIES,
    InventorySources,
    MintClearance,
    Problem,
    RealityInventory,
    _capability_kinds,
    _read_own_cli_source,
    build_reality_inventory,
    clear_mint,
    collect_inventory_sources,
    declares_cli_capability,
    load_reality_inventory,
    make_inventory_reader,
    read_shipped_cli_surface,
    render_reality_inventory,
)
from maestro.tree_retrieval import TREE_TOOL_RESULT_CHAR_CAP, _tree_tool_specs

FAKE_DIR = Path("/fake/reality")

# A small argparse-shaped source exercising every AST branch of
# ``read_shipped_cli_surface``: positional flag/subcommand string literals (kept and
# bound to their owning subcommand via the parser variable), keyword-arg strings
# like help/metavar (dropped), a non-Attribute call (``print``), non-target
# Attribute calls (``ArgumentParser`` / ``add_subparsers``), a positional int
# constant, and a positional Name (both non-string positionals skipped), plus a
# root-parser flag (``--verbose``) attributed to the global surface.
_CLI_FIXTURE_SOURCE = '''
import argparse


def build(_DYNAMIC_FLAG):
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true", help="be loud")
    sub = parser.add_subparsers(dest="command")
    p_cost = sub.add_parser("cost-report", help="Summarize cost data")
    p_cost.add_argument(
        "-d", "--days", type=int, default=7, metavar="N", help="trailing days",
    )
    p_cost.add_argument("--json", action="store_true", help="emit JSON")
    p_run = sub.add_parser("run", help="Execute a goal")
    p_run.add_argument("goal", help="the goal text")
    p_run.add_argument(5)
    p_run.add_argument(_DYNAMIC_FLAG)
    # A chained add_argument on a non-Name receiver (a Call) — the subcommand
    # name is still mined, but the flag cannot be attributed to a variable.
    sub.add_parser("chain").add_argument("--chained")
    print("not an argparse call")
    return parser
'''


def test_shipped_cli_surface_binds_flags_to_owning_subcommand() -> None:
    """`read_shipped_cli_surface` mines positional literals, flags per subcommand.

    The surface is derived by AST-walking add_argument/add_parser calls: leading
    positional string literals only (never keyword-arg strings), with each
    subcommand's flags attributed to *that* subcommand's parser variable and the
    root parser's flags to the global set.
    """
    subcommands, flags_by_sub, global_flags = read_shipped_cli_surface(
        _CLI_FIXTURE_SOURCE,
    )
    assert subcommands == {"cost-report", "run", "chain"}
    # ``chain``'s ``--chained`` flag is NOT attributed — its add_argument
    # receiver is a Call (``sub.add_parser("chain")``), not a parser variable.
    assert flags_by_sub == {
        "cost-report": {"-d", "--days", "--json"},
        "run": {"goal"},
    }
    assert global_flags == {"-v", "--verbose"}
    # Keyword-arg string values must never leak into any bucket.
    for kwarg_string in ("be loud", "Summarize cost data", "N", "emit JSON"):
        assert kwarg_string not in global_flags
        assert all(kwarg_string not in flags for flags in flags_by_sub.values())


def test_shipped_cli_surface_reads_own_cli_by_default() -> None:
    """A bare call parses the real on-disk cli.py and finds known surface."""
    subcommands, flags_by_sub, _global = read_shipped_cli_surface()
    assert "cost-report" in subcommands
    # --json is registered on cost-report specifically, not journal.
    assert "--json" in flags_by_sub.get("cost-report", set())
    assert "--json" not in flags_by_sub.get("journal", set())
    # Argument-group flags alias to their owning subcommand.
    assert "--list" in flags_by_sub["lexicon"]
    assert "--tag-map" in flags_by_sub["lexicon"]
    assert {"--since", "--compare"} <= flags_by_sub["burn-baseline"]
    # Nested sub-subcommands are not top-level surface.
    for nested in ("export", "import", "send", "inbox", "read"):
        assert nested not in subcommands
    assert "email" in subcommands
    assert "self-model" in subcommands


def test_shipped_cli_surface_syntaxerror_returns_empty() -> None:
    """Unparseable source fails open to empty containers (never raises)."""
    assert read_shipped_cli_surface("def (:oops") == (set(), {}, set())


def test_shipped_cli_surface_unreadable_source_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cli.py cannot be read, the guard fails open with empty surface."""
    monkeypatch.setattr(
        "maestro.reality_inventory._read_own_cli_source", lambda: None,
    )
    assert read_shipped_cli_surface() == (set(), {}, set())


def test_read_own_cli_source_oserror_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read failure on cli.py yields ``None`` rather than propagating."""
    def _boom(self: Path, *a: object, **k: object) -> str:
        raise OSError("nope")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert _read_own_cli_source() is None


def test_read_own_cli_source_routes_through_tree_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cli.py is read through the ``tree_retrieval`` door reader (143 owner)."""
    seen: list[tuple[Path, str]] = []

    def _read(root: Path, locator: str) -> str:
        seen.append((root, locator))
        return "src"

    monkeypatch.setattr("maestro.tree_retrieval.read_door_text", _read)
    assert _read_own_cli_source() == "src"
    assert seen[0][1] == "maestro/cli.py"
    assert (seen[0][0] / seen[0][1]).name == "cli.py"


# --- build_reality_inventory -------------------------------------------------


def _finding(rule: str, statement: str) -> AuditFinding:
    return AuditFinding(kind="contradicted_claim", statement=statement, rule=rule)


def _rejection(category: str) -> RejectionRecord:
    return RejectionRecord(
        plan_id="p", title="t", category="capability", priority="medium",
        rejection_reason="r", timestamp="2026-01-01T00:00:00Z",
        rejection_category=category,  # type: ignore[arg-type]
    )


def test_build_prefixes_capabilities_and_problems() -> None:
    """Seeded sources reproduce the known shipped and measured sets."""
    sources = InventorySources(
        cli_subcommands=frozenset({"cost-report"}),
        gate_names=frozenset({"wip_primacy"}),
        audit_rules=frozenset({"stale_entry"}),
        sensor_tags=frozenset({"SETTLED-RELAP"}),
        flag_names=frozenset({"plan_grounding_enabled"}),
        latest_audit=AuditResult(
            iteration=1, timestamp="t",
            findings=[
                _finding("stale_entry", "first"),
                _finding("stale_entry", "second"),
                _finding("orphan", "lone"),
                _finding("", "ruleless finding is skipped"),
            ],
        ),
        sensor_readings=(
            SensorReading("2026-01-02", "SETTLED-RELAP", 3.0, True),
            SensorReading("2026-01-02", "PR-STUCK", 0.0, False),
        ),
        rejections=(
            _rejection("not_needed"),
            _rejection("not_needed"),
            _rejection("wrong_approach"),
            _rejection("other"),
        ),
    )
    inventory = build_reality_inventory(sources)
    assert inventory.shipped_capabilities == frozenset({
        "cli:cost-report",
        "gate:wip_primacy",
        "audit-rule:stale_entry",
        "sensor-tag:SETTLED-RELAP",
        "flag:plan_grounding_enabled",
    })
    assert inventory.measured_problems == (
        Problem("audit:stale_entry", "audit", "first", 2),
        Problem("audit:orphan", "audit", "lone", 1),
        Problem(
            "sensor:SETTLED-RELAP", "sensor",
            "SETTLED-RELAP reads 3 at 2026-01-02", 3,
        ),
        Problem(
            "rejection:not_needed", "rejection",
            "2 recent plan rejection(s) categorised not_needed", 2,
        ),
        Problem(
            "rejection:wrong_approach", "rejection",
            "1 recent plan rejection(s) categorised wrong_approach", 1,
        ),
    )


def test_build_empty_signals_contribute_no_problem() -> None:
    """None/empty audit, all-zero sensors and 'other'-only rejections are silent."""
    for audit in (None, AuditResult(iteration=1, timestamp="t")):
        sources = InventorySources(
            latest_audit=audit,
            sensor_readings=(SensorReading("t", "X", 0.0, False),),
            rejections=(_rejection("other"),),
        )
        inventory = build_reality_inventory(sources)
        assert inventory.measured_problems == ()
        assert inventory.shipped_capabilities == frozenset()


def test_build_only_latest_sensor_timestamp_counts() -> None:
    """A tag crossed only at an older timestamp is not a measured problem."""
    sources = InventorySources(
        sensor_readings=(
            SensorReading("2026-01-01", "OLD", 5.0, True),
            SensorReading("2026-01-02", "NEW", 0.5, True),
            SensorReading("2026-01-02", "OLD", 0.0, False),
        ),
    )
    problems = build_reality_inventory(sources).measured_problems
    assert [p.id for p in problems] == ["sensor:NEW"]
    # A fractional reading still weighs at least one.
    assert problems[0].weight == 1


def test_build_caps_truncate_deterministically() -> None:
    """Problem lists are capped; the capability set is not; summaries are bounded."""
    many = MAX_PROBLEMS_PER_SOURCE + 5
    sources = InventorySources(
        cli_subcommands=frozenset(f"c{i:04d}" for i in range(1000)),
        latest_audit=AuditResult(
            iteration=1, timestamp="t",
            findings=[_finding(f"r{i:02d}", "x" * 500) for i in range(many)],
        ),
    )
    inventory = build_reality_inventory(sources)
    assert len(inventory.shipped_capabilities) == 1000
    assert "cli:c0999" in inventory.shipped_capabilities
    assert len(inventory.measured_problems) == MAX_PROBLEMS_PER_SOURCE
    # Equal weights tie-break on id, so the kept slice is the lexical head.
    assert inventory.measured_problems[0].id == "audit:r00"
    assert inventory.measured_problems[-1].id == f"audit:r{MAX_PROBLEMS_PER_SOURCE - 1:02d}"
    summary = inventory.measured_problems[0].summary
    assert len(summary) == MAX_PROBLEM_SUMMARY_CHARS
    assert summary.endswith("…")
    assert build_reality_inventory(sources) == inventory


def test_build_negative_owned_net_delta_is_not_a_problem() -> None:
    """A shrinking owned backlog is favourable; a growing one is a problem."""
    shrinking = InventorySources(
        sensor_readings=(SensorReading("t", "OWNED-NET-DELTA", -3.0, True),),
    )
    assert build_reality_inventory(shrinking).measured_problems == ()
    growing = InventorySources(
        sensor_readings=(SensorReading("t", "OWNED-NET-DELTA", 2.0, True),),
    )
    problems = build_reality_inventory(growing).measured_problems
    assert [p.id for p in problems] == ["sensor:OWNED-NET-DELTA"]
    assert problems[0].weight == 2


def test_build_audit_summary_is_single_line() -> None:
    """A multi-line finding statement collapses to one whitespace-normalised line."""
    sources = InventorySources(
        latest_audit=AuditResult(
            iteration=1, timestamp="t",
            findings=[_finding("r", "first line\n  second\tline")],
        ),
    )
    problems = build_reality_inventory(sources).measured_problems
    assert problems[0].summary == "first line second line"


# --- collect_inventory_sources / load_reality_inventory ----------------------

_STUB_LEXICON = (
    "| term | definition | names | anchor | variants | minted |\n"
    "|---|---|---|---|---|---|\n"
    "| settled-relap | d | n | `classify_settled_relap` | SETTLED-RELAP "
    "| behavioral-sensor tag map |\n"
)


def _seed_memory(memory: Memory) -> None:
    memory.append_audit_result(AuditResult(
        iteration=1, timestamp="2026-01-01T00:00:00Z",
        findings=[_finding("stale_entry", "old")],
    ))
    memory.append_audit_result(AuditResult(
        iteration=2, timestamp="2026-01-02T00:00:00Z",
        findings=[_finding("orphan", "new")],
    ))
    memory.record_sensor_readings([
        SensorReading("2026-01-02T00:00:00Z", "SETTLED-RELAP", 2.0, True),
    ])
    memory.record_rejection(_rejection("not_needed"))


def test_collect_sources_from_seeded_state(mem_fs: dict[str, str]) -> None:
    """The collector reads every owning registry and ledger."""
    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    config.ensure_dirs()
    mem_fs[str(FAKE_DIR / "docs" / "lexicon.md")] = _STUB_LEXICON
    memory = Memory(config.memory_dir)
    _seed_memory(memory)
    sources = collect_inventory_sources(
        config, memory, rejection_window=10, sensor_window=10,
    )
    assert "cost-report" in sources.cli_subcommands
    assert "wip_primacy" in sources.gate_names
    assert sources.audit_rules == frozenset(RULE_AUTHORITY)
    assert sources.sensor_tags == frozenset({"SETTLED-RELAP"})
    assert "plan_grounding_enabled" in sources.flag_names
    assert sources.latest_audit is not None
    assert sources.latest_audit.iteration == 2
    assert [r.tag for r in sources.sensor_readings] == ["SETTLED-RELAP"]
    assert [r.rejection_category for r in sources.rejections] == ["not_needed"]


def test_collect_sources_missing_lexicon_yields_no_tags(
    mem_fs: dict[str, str],
) -> None:
    """Absent docs/lexicon.md → empty sensor-tag set; empty ledgers → no signal."""
    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    sources = collect_inventory_sources(
        config, memory, rejection_window=10, sensor_window=10,
    )
    assert sources.sensor_tags == frozenset()
    assert sources.latest_audit is None
    assert sources.sensor_readings == ()
    assert sources.rejections == ()


def test_load_reality_inventory_composes(mem_fs: dict[str, str]) -> None:
    """The convenience entry point builds over the collected sources."""
    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    _seed_memory(memory)
    inventory = load_reality_inventory(config, memory)
    assert "cli:reality-inventory" in inventory.shipped_capabilities
    assert [p.id for p in inventory.measured_problems] == [
        "audit:orphan", "sensor:SETTLED-RELAP", "rejection:not_needed",
    ]


# ── clear_mint (proposal 136 generator reality grounding) ────────────────────

_INVENTORY = RealityInventory(
    shipped_capabilities=frozenset({"cli:reality-inventory", "flag:foo_enabled"}),
    measured_problems=(
        Problem(id="audit:rule_x", source="audit", summary="s", weight=1),
    ),
)
_CORPUS = "def already_defined(x):\n    return x\n"
_CLEARED = MintClearance(cleared=True, reason=None, offending=None)


@pytest.mark.parametrize(
    "name", ["cli:reality-inventory", "reality-inventory", "already_defined"],
)
def test_clear_mint_refuses_shipped_name(name: str) -> None:
    """Prefixed-id, bare-suffix and corpus-definition hits each refuse duplicate."""
    verdict = clear_mint(
        _INVENTORY, problem_ref=None, new_capability_names=[name],
        corpus_text=_CORPUS,
    )
    assert verdict == MintClearance(cleared=False, reason="duplicate", offending=name)


def test_clear_mint_prefixed_novel_name_clears() -> None:
    """A prefixed name absent from the set clears even if its suffix is defined."""
    verdict = clear_mint(
        _INVENTORY, problem_ref=None,
        new_capability_names=["gate:already_defined", "  ", "brand_new_thing"],
        corpus_text=_CORPUS,
    )
    assert verdict == _CLEARED


def test_clear_mint_problem_ref() -> None:
    """A measured problem clears; an unmeasured one refuses."""
    measured = clear_mint(
        _INVENTORY, problem_ref="audit:rule_x", new_capability_names=[],
        corpus_text="",
    )
    assert measured == _CLEARED
    unmeasured = clear_mint(
        _INVENTORY, problem_ref="sensor:NOPE", new_capability_names=None,
        corpus_text="",
    )
    assert unmeasured == MintClearance(
        cleared=False, reason="unmeasured-problem", offending="sensor:NOPE",
    )


def test_clear_mint_neither_field_clears() -> None:
    """The open-ended escape hatch: nothing declared clears unconditionally."""
    verdict = clear_mint(
        _INVENTORY, problem_ref=None, new_capability_names=None, corpus_text=_CORPUS,
    )
    assert verdict == _CLEARED


def test_clear_mint_caps_names_and_truncates_offending() -> None:
    """Names past the cap are untested; the offending echo is bounded."""
    padding = [f"novel_{i}" for i in range(MAX_CLEARANCE_NAMES)]
    beyond_cap = clear_mint(
        _INVENTORY, problem_ref=None,
        new_capability_names=[*padding, "reality-inventory"], corpus_text="",
    )
    assert beyond_cap == _CLEARED
    long_ref = "x" * (MAX_OFFENDING_CHARS + 50)
    verdict = clear_mint(
        _INVENTORY, problem_ref=long_ref, new_capability_names=None, corpus_text="",
    )
    assert verdict.offending == long_ref[:MAX_OFFENDING_CHARS]


@pytest.mark.parametrize(
    "name", ["to_dict", "__init__", "Cls.to_dict", "kwarg_name"],
)
def test_clear_mint_ignores_method_kwarg_dotted_and_dunder(name: str) -> None:
    """Only a column-zero definition refuses; methods/kwargs/dotted/dunders clear."""
    corpus = (
        "class Existing:\n"
        "    def to_dict(self):\n"
        "        return {}\n"
        "    def __init__(self):\n"
        "        call(kwarg_name=1)\n"
        "def __init__():\n"
        "    pass\n"
    )
    verdict = clear_mint(
        _INVENTORY, problem_ref=None, new_capability_names=[name],
        corpus_text=corpus,
    )
    assert verdict == _CLEARED


@pytest.mark.parametrize(
    "corpus", ["class Shipped:\n", "async def Shipped():\n", "Shipped: int = 1\n"],
)
def test_clear_mint_refuses_module_level_definitions(corpus: str) -> None:
    """Every module-level definition shape refuses ``duplicate``."""
    verdict = clear_mint(
        _INVENTORY, problem_ref=None, new_capability_names=["Shipped"],
        corpus_text=corpus,
    )
    assert verdict.reason == "duplicate"


def test_clear_mint_problem_ref_past_prompt_cap_is_measured() -> None:
    """A measured problem ranked past ``MAX_PROBLEMS_PER_SOURCE`` still clears."""
    findings = [
        _finding(f"rule_{i:02d}", "s") for i in range(MAX_PROBLEMS_PER_SOURCE + 1)
    ]
    inventory = build_reality_inventory(
        InventorySources(
            latest_audit=AuditResult(iteration=1, timestamp="t", findings=findings),
        ),
    )
    capped_ids = {problem.id for problem in inventory.measured_problems}
    last = f"audit:rule_{MAX_PROBLEMS_PER_SOURCE:02d}"
    assert last not in capped_ids
    verdict = clear_mint(
        inventory, problem_ref=last, new_capability_names=None, corpus_text="",
    )
    assert verdict == _CLEARED


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (None, False),
        ([], False),
        ([" cli:x "], True),
        (["gate:x", "foo"], False),
        ([" cli: "], False),
        (["cli:a b"], False),
        ([f"n{i}" for i in range(MAX_CLEARANCE_NAMES)] + ["cli:late"], False),
    ],
)
def test_declares_cli_capability(names: list[str] | None, expected: bool) -> None:
    """Same strip-and-cap rule as ``clear_mint``; only ``cli:`` counts."""
    assert declares_cli_capability(names) is expected


# --- render_reality_inventory / make_inventory_reader ------------------------


def _render_inventory(caps: list[str]) -> RealityInventory:
    return RealityInventory(
        shipped_capabilities=frozenset(caps),
        measured_problems=(
            Problem(id="audit:r1", source="audit", summary="first", weight=4),
            Problem(id="sensor:TAG", source="sensor", summary="tag", weight=1),
        ),
    )


def test_render_default_view_lists_problems_and_counts() -> None:
    text = render_reality_inventory(
        _render_inventory(["cli:status", "cli:cost-report", "flag:x"]), None,
    )
    assert text.splitlines() == [
        "shipped capabilities by kind:",
        "cli: 2",
        "gate: 0",
        "audit-rule: 0",
        "sensor-tag: 0",
        "flag: 1",
        "list one kind's names with kind = one of: "
        "cli, gate, audit-rule, sensor-tag, flag",
        "measured problems (cite one id as problem_ref):",
        "audit:r1 (weight 4): first",
        "sensor:TAG (weight 1): tag",
    ]


def test_render_default_view_keeps_kinds_inside_result_cap() -> None:
    """A long problem list truncated at the tool cap never drops the kind line."""
    problems = tuple(
        Problem(id=f"audit:r{i}", source="audit", summary="s" * 160, weight=1)
        for i in range(80)
    )
    inventory = RealityInventory(
        shipped_capabilities=frozenset(), measured_problems=problems,
    )
    text = render_reality_inventory(inventory, None)
    assert len(text) > TREE_TOOL_RESULT_CHAR_CAP
    assert "list one kind's names with kind" in text[:TREE_TOOL_RESULT_CHAR_CAP]


def test_inventory_tool_description_names_every_kind() -> None:
    """The tool description's kind vocabulary cannot drift from the prefixes."""
    specs = _tree_tool_specs(MagicMock())
    description = next(d for n, d, _, _ in specs if n == "read_reality_inventory")
    for kind in _capability_kinds():
        assert kind in description


def test_render_default_view_without_problems() -> None:
    empty = RealityInventory(shipped_capabilities=frozenset(), measured_problems=())
    lines = render_reality_inventory(empty, None).splitlines()
    assert lines[-1] == "-"


def test_render_kind_view_lists_sorted_bare_names() -> None:
    text = render_reality_inventory(
        _render_inventory(["cli:status", "cli:cost-report", "flag:x"]), "cli",
    )
    assert text == "shipped cli (2):\ncost-report\nstatus"


def test_render_kind_view_truncates_at_cap() -> None:
    caps = [f"gate:g{i:04d}" for i in range(MAX_RENDERED_CAPABILITIES + 5)]
    lines = render_reality_inventory(_render_inventory(caps), "gate").splitlines()
    assert len(lines) == MAX_RENDERED_CAPABILITIES + 2
    assert lines[-1] == "[truncated — 5 more names]"


def test_render_unknown_kind_is_refused() -> None:
    text = render_reality_inventory(_render_inventory([]), "bogus")
    assert text == (
        "refused: unknown kind 'bogus'; valid kinds: "
        "cli, gate, audit-rule, sensor-tag, flag"
    )


def test_inventory_reader_loads_once_per_reader() -> None:
    inventory = _render_inventory(["cli:status"])
    with patch(
        "maestro.reality_inventory.load_reality_inventory", return_value=inventory,
    ) as load:
        reader = make_inventory_reader(MagicMock(), MagicMock())
        load.assert_not_called()
        first = reader("cli")
        second = reader(None)
    load.assert_called_once()
    assert first == "shipped cli (1):\nstatus"
    assert second.startswith("shipped capabilities by kind:")


def test_inventory_reader_oserror_is_refused_then_retried() -> None:
    inventory = _render_inventory(["cli:status"])
    with patch(
        "maestro.reality_inventory.load_reality_inventory",
        side_effect=[OSError("disk"), inventory],
    ) as load:
        reader = make_inventory_reader(MagicMock(), MagicMock())
        refused = reader(None)
        recovered = reader("cli")
    assert refused == "refused: reality inventory unavailable"
    assert recovered == "shipped cli (1):\nstatus"
    assert load.call_count == 2
