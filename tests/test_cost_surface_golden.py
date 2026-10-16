"""Golden pins for the cost and CLI surfaces left after the revenue lineage.

Each test pins a surface to its exact shape, so any drift turns red. All
in-memory.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields, replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from maestro import cli
from maestro.audit_checks import _DOOR_REFUSAL_REASON_TABLE
from maestro.caller_spend import read_spend
from maestro.config import Config
from maestro.cost_tracking import (
    CostSample,
    _clear_cost_rate_cache,
    build_budget_breakdown,
    build_cost_rates,
)

_OLD_COST_SAMPLE_FIELDS = [
    "timestamp", "cost_usd", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "caller", "duration_ms",
    "estimated", "unmetered", "provider", "effort", "model",
]
_DOOR_FAMILIES = [
    "saturation", "grounding", "restatement", "value", "flag-graduation", "cooldown",
]
_SUBCOMMANDS = frozenset({
    "_gate-bash-hook",
    "_mcp-serve",
    "audit-flags",
    "burn-baseline",
    "consciousness-report",
    "contemplate",
    "cost-per-task",
    "cost-report",
    "deed-ledger",
    "docs",
    "email",
    "evolve",
    "gate-check",
    "gate-classify",
    "gate-lease",
    "gate-scope",
    "history",
    "issues",
    "journal",
    "kill",
    "lexicon",
    "list-gates",
    "list-limitations",
    "list-proposals",
    "list-versions",
    "logs",
    "loop",
    "measure-confirmed-landings",
    "measure-dead-on-arrival-mints",
    "measure-false-success",
    "measure-fork-authorship",
    "measure-gate-contention",
    "measure-gate-triggers",
    "measure-handoff-relapse",
    "measure-noops",
    "measure-outcome-trends",
    "measure-phantom-landings",
    "measure-plan-mill",
    "measure-planning-grounding",
    "measure-premise-accuracy",
    "measure-rejection-classes",
    "measure-relap",
    "messages",
    "notify-pre-deploy",
    "owned-acts",
    "pr-lap-ledger",
    "pr-laps",
    "pr-owed-act",
    "preflight",
    "publications",
    "reality-inventory",
    "resume-task",
    "reviews",
    "run",
    "runtime-contract",
    "self-model",
    "serve",
    "show-plan",
    "shutdown",
    "source-budget",
    "status",
    "tool-surface-audit",
    "workspace-counterfactual",
})
def test_cost_sample_fields_gain_only_billing() -> None:
    assert [f.name for f in fields(CostSample)] == [*_OLD_COST_SAMPLE_FIELDS, "billing"]


class _Backend:
    def __init__(self, samples: list[CostSample]) -> None:
        self._samples = samples

    def samples_in_window(
        self, window_hours: float, *, now: datetime | None = None,
    ) -> list[CostSample]:
        return list(self._samples)

    def cache_key(self) -> tuple[str, int]:
        return ("golden", id(self._samples))


def _samples() -> list[CostSample]:
    now = datetime.now(UTC).isoformat()
    return [
        CostSample(now, 2.0, 1, 1, 0, 0, "reflect", 10, provider="anthropic"),
        CostSample(now, 3.0, 1, 1, 0, 0, "propose_plan", 10, provider="openai"),
    ]


def test_list_price_arithmetic_ignores_billing() -> None:
    plain = _samples()
    stamped = [replace(plain[0], billing="metered"), plain[1]]
    _clear_cost_rate_cache()
    rates = build_cost_rates(_Backend(plain), window_hours=24.0)  # type: ignore[arg-type]
    budget = build_budget_breakdown(_Backend(plain), window_hours=24.0)  # type: ignore[arg-type]
    _clear_cost_rate_cache()
    assert build_cost_rates(_Backend(stamped), window_hours=24.0) == rates  # type: ignore[arg-type]
    assert build_budget_breakdown(_Backend(stamped), window_hours=24.0) == budget  # type: ignore[arg-type]


def test_read_spend_shape() -> None:
    reading = read_spend(_Backend(_samples()), window_days=30, iterations=2)  # type: ignore[arg-type]
    data = reading.to_dict()
    assert sorted(data) == sorted([
        "window_days", "requested_window_days", "iterations", "spend_usd",
        "spend_usd_per_day", "spend_usd_per_iteration", "by_caller",
        "spend_source",
    ])
    assert [row["caller"] for row in data["by_caller"]] == ["propose_plan", "reflect"]


def _parser() -> argparse.ArgumentParser:
    captured: dict[str, Any] = {}

    def _capture(self: argparse.ArgumentParser, *_: object, **__: object) -> None:
        captured["parser"] = self
        raise SystemExit(0)

    with patch.object(argparse.ArgumentParser, "parse_args", _capture), \
            patch.object(sys, "argv", ["maestro"]), \
            pytest.raises(SystemExit):
        cli.main()
    parser = captured["parser"]
    assert isinstance(parser, argparse.ArgumentParser)
    return parser


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("no subparsers")


def test_subcommands_carry_no_revenue_surface() -> None:
    assert frozenset(_subparsers(_parser())) == _SUBCOMMANDS


def test_door_refusal_families() -> None:
    """The charter-key family went with the register keys; value stays for history."""
    families = [family for family, _ in _DOOR_REFUSAL_REASON_TABLE]
    assert families == _DOOR_FAMILIES


def test_disabled_archive_still_validates() -> None:
    Config(archive_threshold_days=0).validate()
