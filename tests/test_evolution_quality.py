"""Guards for the quality-pipeline split out of ``maestro/evolution.py`` (proposal 147)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_quality
from maestro.evolution import EvolutionEngine, QualityGateResult


def test_run_quality_gates_is_bound_to_the_module_function() -> None:
    # Bound by assignment (not a forwarding def), so the engine attribute *is*
    # the module function and an instance call passes the engine as ``engine``.
    assert EvolutionEngine.run_quality_gates is evolution_quality.run_quality_gates


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "kwargs"),
    [
        ("converge_to_green", ("plan", "session"), {"ceiling": 3, "base_hash": "abc"}),
        ("self_review", ("plan", "abc", "session"), {}),
        (
            "auto_fix",
            ("plan", "review", "session"),
            {
                "round_n": 1, "max_rounds": 2, "long_context": True,
                "bypass_warning_throttle": True, "attribution": None,
            },
        ),
    ],
)
async def test_async_delegators_forward_to_module(
    name: str, args: tuple[Any, ...], kwargs: dict[str, Any],
) -> None:
    engine = MagicMock(spec=EvolutionEngine)
    with patch.object(evolution_quality, name, new_callable=AsyncMock) as spy:
        await getattr(EvolutionEngine, name)(engine, *args, **kwargs)
    spy.assert_awaited_once_with(engine, *args, **kwargs)


def test_find_uncalled_symbols_delegates_to_module() -> None:
    engine = MagicMock(spec=EvolutionEngine)
    with patch.object(evolution_quality, "_find_uncalled_symbols", return_value=["x"]) as spy:
        assert EvolutionEngine._find_uncalled_symbols(engine, "diff") == ["x"]
    spy.assert_called_once_with(engine, "diff")


def test_static_progress_delegators_stay_callable_on_the_class() -> None:
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True, raw_output="ruff failed",
    )
    assert EvolutionEngine._progress_signals(review) == evolution_quality._progress_signals(review)
    assert EvolutionEngine._progress_counts(review) == evolution_quality._progress_counts(review)
