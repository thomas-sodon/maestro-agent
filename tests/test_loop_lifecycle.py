"""Collaborator-direct tests for ``maestro.loop.lifecycle`` (068 step 3).

These construct :class:`LifecycleController` against a narrow stub agent
instead of a full ``MaestroAgent``, proving the cadence/throttle cluster
is separately testable. Behavioural equivalence with the pre-extraction
code is carried by the unchanged ``tests/test_agent*.py`` suites, which
drive the same bodies through the delegating shims.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.loop.lifecycle import (
    _BUDGET_GATED_PHASES,
    LifecycleController,
    _format_status_message,
)
from maestro.work_target import WorkFraming, WorkTarget


def _work_target(focus_profile: WorkFraming = "self_evolution") -> WorkTarget:
    """Self-defaulting ``WorkTarget`` for the stub; only ``focus_profile`` varies.

    ``phase_will_run`` resolves ``agent.config.work_target()`` for the focus
    dial, so the stub config must expose one. Every other facet holds its self
    default — the dial reads ``focus_profile`` alone.
    """
    return WorkTarget(
        repo_workspace=Path("/fake/workspace"),
        github_repo="owner/repo",
        gate_source_path="maestro/",
        gate_tests_path="tests/",
        gate_lint_extra_paths=("scripts/",),
        task_framing="self_evolution",
        focus_profile=focus_profile,
    )


def _stub_agent(
    *, focus_profile: WorkFraming = "self_evolution", **overrides: Any
) -> SimpleNamespace:
    """Minimal duck-typed agent exposing only what the controller touches."""
    stub = SimpleNamespace(
        config=SimpleNamespace(
            max_loop_budget_usd_per_hour=0.0,
            lifecycle_path=Path("/fake/lifecycle.json"),
            artifact_review_enabled=False,
            publication_enabled=False,
            tend_prs_enabled=False,
            self_directed_tasks_enabled=True,
            work_target=lambda: _work_target(focus_profile),
        ),
        budget_throttle_active=False,
        burn_rate_throttle_active=False,
        consciousness=MagicMock(),
        journal=MagicMock(),
        memory=MagicMock(),
        _iter_message_context="",
        _last_phase_iteration={},
        _budget_throttle_entered_at=None,
        _has_runnable_work=MagicMock(return_value=True),
        _notify_best_effort=AsyncMock(),
    )
    for name, value in overrides.items():
        setattr(stub, name, value)
    return stub


def _controller(stub: SimpleNamespace) -> LifecycleController:
    return LifecycleController(stub)  # type: ignore[arg-type]


def _cadence_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "iteration": 4,
        "smoke": False,
        "first_iteration_after_restart": False,
        "task_every": 2,
        "evolve_every": 5,
        "dream_every": 10,
        "audit_self_every": 3,
        "contemplate_every": 1,
        "body_frozen": False,
    }
    base.update(overrides)
    return base


# --- phase_will_run -----------------------------------------------------


def test_phase_will_run_budget_gate_skips_gated_phase() -> None:
    """A budget throttle skips every ``_BUDGET_GATED_PHASES`` member."""
    stub = _stub_agent(budget_throttle_active=True)
    ctrl = _controller(stub)
    for phase in _BUDGET_GATED_PHASES:
        assert ctrl.phase_will_run(phase, **_cadence_kwargs()) is False


def test_phase_will_run_body_frozen_gates_despite_live_release() -> None:
    """``body_frozen`` keeps every gated phase skipped after a live release."""
    stub = _stub_agent(budget_throttle_active=False)
    ctrl = _controller(stub)
    for phase in _BUDGET_GATED_PHASES:
        assert ctrl.phase_will_run(
            phase, **_cadence_kwargs(body_frozen=True),
        ) is False


def _with_live_statuses(*statuses: str) -> SimpleNamespace:
    stub = _stub_agent()
    stub.memory.get_live_plan_records.return_value = [
        SimpleNamespace(status=status) for status in statuses
    ]
    return stub


def test_check_merged_plans_runs_while_a_plan_is_implemented() -> None:
    """A12 (proposal 155 step 4): an implemented plan arms the merge check on
    any iteration, so a merged gap plan is marked merged within one tick."""
    ctrl = _controller(_with_live_statuses("approved", "implemented"))
    assert ctrl.phase_will_run("check_merged_plans", **_cadence_kwargs()) is True


def test_check_merged_plans_stays_off_without_an_implemented_plan() -> None:
    ctrl = _controller(_with_live_statuses("pending_review", "approved"))
    assert ctrl.phase_will_run("check_merged_plans", **_cadence_kwargs()) is False
    assert ctrl.phase_will_run(
        "check_merged_plans", **_cadence_kwargs(first_iteration_after_restart=True),
    ) is True


def test_check_merged_plans_never_runs_in_smoke() -> None:
    ctrl = _controller(_with_live_statuses("implemented"))
    assert ctrl.phase_will_run(
        "check_merged_plans", **_cadence_kwargs(smoke=True),
    ) is False


def test_phase_will_run_cadence_modulus() -> None:
    """Cadence phases fire only on their modulus iterations."""
    ctrl = _controller(_stub_agent())
    assert ctrl.phase_will_run("generate_task", **_cadence_kwargs(iteration=4)) is True
    assert ctrl.phase_will_run("generate_task", **_cadence_kwargs(iteration=5)) is False
    assert ctrl.phase_will_run("evolve", **_cadence_kwargs(iteration=5)) is True
    assert ctrl.phase_will_run("evolve", **_cadence_kwargs(iteration=4)) is False


def test_phase_will_run_self_directed_switch_gates_generate_task() -> None:
    """Proposal 116: flag-off never schedules ``generate_task`` on any
    iteration, while ``run_task`` stays scheduled given runnable work; flag-on
    leaves the ``task_every`` cadence unchanged."""
    # Flag off — ``generate_task`` is starved on every iteration, including
    # the ones its cadence modulus would otherwise fire on.
    stub = _stub_agent()
    stub.config.self_directed_tasks_enabled = False
    ctrl = _controller(stub)
    for iteration in (2, 4, 6, 8, 100):
        run = ctrl.phase_will_run(
            "generate_task", **_cadence_kwargs(iteration=iteration, task_every=2)
        )
        assert run is False
    # ``run_task`` is untouched by the switch: still scheduled when
    # ``_has_runnable_work`` reports pending work.
    assert ctrl.phase_will_run("run_task", **_cadence_kwargs()) is True

    # Flag on — the ``task_every`` cadence is unchanged.
    on = _controller(_stub_agent())
    assert on.phase_will_run(
        "generate_task", **_cadence_kwargs(iteration=4, task_every=2)
    ) is True
    assert on.phase_will_run(
        "generate_task", **_cadence_kwargs(iteration=5, task_every=2)
    ) is False


def test_phase_will_run_unknown_phase_defaults_true() -> None:
    """Unknown phase names run noisily rather than silently skipping."""
    ctrl = _controller(_stub_agent())
    assert ctrl.phase_will_run("brand_new_phase", **_cadence_kwargs()) is True


def test_phase_will_run_audit_dedupes_frozen_iteration() -> None:
    """``_last_phase_iteration`` suppresses a re-fire on a frozen counter."""
    stub = _stub_agent(_last_phase_iteration={"audit_self_model": 3})
    ctrl = _controller(stub)
    kwargs = _cadence_kwargs(iteration=3, audit_self_every=3)
    assert ctrl.phase_will_run("audit_self_model", **kwargs) is False
    stub._last_phase_iteration.clear()
    assert ctrl.phase_will_run("audit_self_model", **kwargs) is True


def test_phase_will_run_contemplate_cadence_gate() -> None:
    """``contemplate`` is cadence-gated: default 1 runs always; a raised cadence
    fires only on multiples; smoke bypasses the cadence; a throttle still skips it."""
    ctrl = _controller(_stub_agent())
    # Default cadence of 1 fires on every iteration.
    for iteration in (1, 2, 3, 7, 100):
        run = ctrl.phase_will_run("contemplate", **_cadence_kwargs(iteration=iteration))
        assert run is True
    # A raised cadence fires only on multiples of ``contemplate_every``.
    assert ctrl.phase_will_run(
        "contemplate", **_cadence_kwargs(iteration=7, contemplate_every=5)
    ) is False
    assert ctrl.phase_will_run(
        "contemplate", **_cadence_kwargs(iteration=10, contemplate_every=5)
    ) is True
    # ``0`` disables the phase entirely, even on iteration 0.
    assert ctrl.phase_will_run(
        "contemplate", **_cadence_kwargs(iteration=0, contemplate_every=0)
    ) is False
    # ``smoke=True`` returns True regardless of an off-cadence position, so the
    # deploy iteration always validates the contemplate SDK path end-to-end.
    assert ctrl.phase_will_run(
        "contemplate", **_cadence_kwargs(iteration=7, contemplate_every=5, smoke=True)
    ) is True


def test_phase_will_run_contemplate_skipped_under_budget_throttle() -> None:
    """A budget throttle skips ``contemplate`` before the cadence branch is reached."""
    stub = _stub_agent(budget_throttle_active=True)
    ctrl = _controller(stub)
    # On-cadence (default 1) it would otherwise run — the throttle wins.
    assert ctrl.phase_will_run("contemplate", **_cadence_kwargs(iteration=4)) is False


def test_phase_will_run_focus_dial_suppresses_introspection() -> None:
    """``external_delivery`` drops the introspective phases; delivery keeps its slots."""
    stub = _stub_agent(focus_profile="external_delivery")
    stub.config.tend_prs_enabled = True
    stub.config.tend_prs_every = 5
    ctrl = _controller(stub)
    kwargs = _cadence_kwargs(
        iteration=10,
        evolve_every=5,
        dream_every=10,
        audit_self_every=5,
        curate_every=5,
        curate_enabled=True,
    )
    # The three fully-introspective phases would all fire at iteration 10 under
    # the default profile; the dial suppresses every one of them.
    for phase in ("evolve", "audit_self_model", "curate_pending_plans"):
        assert ctrl.phase_will_run(phase, **kwargs) is False
    # The zero-SDK dream phase is not suppressed (proposal 149 Step 5).
    assert ctrl.phase_will_run("dream", **kwargs) is True
    # Delivery phases keep their iteration slots.
    assert ctrl.phase_will_run("run_task", **kwargs) is True
    assert ctrl.phase_will_run("tend_evolution_prs", **kwargs) is True
    assert ctrl.phase_will_run("generate_task", **kwargs) is True


def test_phase_will_run_focus_dial_default_profile_unchanged() -> None:
    """The default ``self_evolution`` profile leaves every cadence exactly as before."""
    stub = _stub_agent()  # focus_profile defaults to self_evolution
    ctrl = _controller(stub)
    kwargs = _cadence_kwargs(
        iteration=10,
        evolve_every=5,
        dream_every=10,
        audit_self_every=5,
        curate_every=5,
        curate_enabled=True,
    )
    for phase in ("evolve", "dream", "audit_self_model", "curate_pending_plans"):
        assert ctrl.phase_will_run(phase, **kwargs) is True


def test_phase_will_run_focus_dial_leaves_non_suppressed_cadence_intact() -> None:
    """The dial suppresses only its set — a non-suppressed phase keeps its cadence.

    This is the assertion with teeth: the three suppressed phases return ``False``
    *before* any cadence check, so asserting on them alone cannot prove the dial
    leaves the cadence logic untouched. ``generate_task`` is **not** in the
    suppressed set, so under ``external_delivery`` its own modulus still decides —
    off-cadence stays ``False`` (the dial never forces it on) and on-cadence stays
    ``True`` (the dial never suppresses a delivery phase).
    """
    stub = _stub_agent(focus_profile="external_delivery")
    ctrl = _controller(stub)
    # task_every=5 → generate_task is off-cadence at iter 7, on-cadence at 10.
    off = _cadence_kwargs(iteration=7, task_every=5)
    on = _cadence_kwargs(iteration=10, task_every=5)
    assert ctrl.phase_will_run("generate_task", **off) is False
    assert ctrl.phase_will_run("generate_task", **on) is True
    # The suppressed phases stay suppressed regardless of cadence position.
    for phase in ("evolve", "audit_self_model", "curate_pending_plans"):
        assert ctrl.phase_will_run(phase, **off) is False


# --- compute_budget_throttle_multiplier ----------------------------------


def test_throttle_multiplier_disabled_budget_is_neutral() -> None:
    """``max_loop_budget_usd_per_hour <= 0`` always returns the neutral tuple."""
    ctrl = _controller(_stub_agent())
    assert ctrl.compute_budget_throttle_multiplier(False) == (1.0, 1.0, None)


def test_throttle_multiplier_smoke_is_neutral() -> None:
    stub = _stub_agent()
    stub.config.max_loop_budget_usd_per_hour = 1.0
    ctrl = _controller(stub)
    assert ctrl.compute_budget_throttle_multiplier(True) == (1.0, 1.0, None)


# --- maybe_notify_maintenance_transition ---------------------------------


@pytest.mark.asyncio
async def test_maintenance_transition_fires_once_per_edge() -> None:
    """Entry fires on the rising edge only; sustained state stays quiet."""
    stub = _stub_agent(
        _maintenance_mode_active=True,
        _maintenance_milestone_persisted=False,
    )
    ctrl = _controller(stub)
    await ctrl.maybe_notify_maintenance_transition()
    assert stub._maintenance_milestone_persisted is True
    stub._notify_best_effort.assert_awaited_once()

    stub._notify_best_effort.reset_mock()
    await ctrl.maybe_notify_maintenance_transition()
    stub._notify_best_effort.assert_not_awaited()


# --- read_lifecycle_progress ----------------------------------------------


def test_read_lifecycle_progress_missing_file_yields_empty_tuple(
    mem_fs: dict[str, str],
) -> None:
    stub = _stub_agent()
    stub.config.lifecycle_path = Path("/fake/none/lifecycle.json")
    result = _controller(stub).read_lifecycle_progress()
    assert result == (
        None, 0, None, None, False, "", "", None,
        None, None, None, False, None,
        0, 0, 0, 0,
        False,
    )


@pytest.mark.parametrize("payload", ["[]", '"x"', "42", "null"])
def test_read_lifecycle_progress_non_dict_top_level_yields_empty_tuple(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    payload: str,
) -> None:
    """Valid-but-non-object JSON falls back to the empty default, loudly.

    Regression for #2762: ``json.loads`` parses ``[]``/``"x"``/``42``/``null``
    successfully, but the immediate ``data.get("last_shutdown")`` then raised
    ``AttributeError`` — a type the outer ``except`` does not list — crashing
    loop startup instead of honouring the reader's documented corruption
    fallback. The top-level ``isinstance(data, dict)`` guard raises
    ``ValueError`` (already in the ``except`` tuple), routing wrong-shape JSON
    to the same all-empty tuple as a missing file.
    """
    stub = _stub_agent()
    stub.config.lifecycle_path = Path("/fake/nondict/lifecycle.json")
    mem_fs["/fake/nondict/lifecycle.json"] = payload
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = _controller(stub).read_lifecycle_progress()
    assert result == (
        None, 0, None, None, False, "", "", None,
        None, None, None, False, None,
        0, 0, 0, 0,
        False,
    )
    assert "Non-dict top-level lifecycle.json" in caplog.text


def test_read_lifecycle_progress_reads_cursor(mem_fs: dict[str, str]) -> None:
    stub = _stub_agent()
    stub.config.lifecycle_path = Path("/fake/lc/lifecycle.json")
    mem_fs["/fake/lc/lifecycle.json"] = json.dumps({
        "iteration": 7,
        "next_phase": "contemplate",
        "message_context": "ctx",
    })
    result = _controller(stub).read_lifecycle_progress()
    assert result[1] == 7
    assert result[2] == "contemplate"
    # No ``last_shutdown`` + in-progress evidence → crashed.
    assert result[4] is True
    assert result[5] == "ctx"


def test_read_lifecycle_progress_corrupt_iteration_preserves_last_shutdown(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt ``iteration`` defaults to 0 without discarding the record.

    Regression for #430: a non-numeric ``iteration`` string passed the old
    ``isinstance(..., int | str)`` guard, so ``int("abc")`` still raised
    ``ValueError`` inside ``read_lifecycle_progress``. The method's
    last-resort ``except`` swallowed it by returning the all-empty tuple,
    nuking the valid ``last_shutdown`` and the resume cursor. The per-field
    ``_coerce_lifecycle_int`` confines that failure to ``iteration``.
    """
    stub = _stub_agent()
    stub.config.lifecycle_path = Path("/fake/corrupt-iter/lifecycle.json")
    mem_fs["/fake/corrupt-iter/lifecycle.json"] = json.dumps({
        "last_shutdown": "2026-04-06T21:00:00+00:00",
        "iteration": "abc",
        "next_phase": "contemplate",
    })
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = _controller(stub).read_lifecycle_progress()
    # last_shutdown survives the corrupt iteration...
    assert result[0] == "2026-04-06T21:00:00+00:00"
    # ...the corrupt cursor defaults to 0...
    assert result[1] == 0
    # ...next_phase survives...
    assert result[2] == "contemplate"
    # ...and a preserved last_shutdown means not crashed.
    assert result[4] is False
    # ...and the fail-loud warning names the corrupt field.
    assert "Corrupt iteration in lifecycle.json" in caplog.text


def test_read_lifecycle_progress_corrupt_counter_preserves_last_shutdown(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt ``consecutive_*`` counter defaults to 0, sparing the record.

    Same #430 failure mode for the four consecutive-failure counters: a
    corrupt counter string raised ``ValueError`` and discarded
    ``last_shutdown`` via the outer ``except``. ``_coerce_lifecycle_int``
    keeps the shutdown timestamp and iteration cursor intact.
    """
    stub = _stub_agent()
    stub.config.lifecycle_path = Path("/fake/corrupt-counter/lifecycle.json")
    mem_fs["/fake/corrupt-counter/lifecycle.json"] = json.dumps({
        "last_shutdown": "2026-04-06T21:00:00+00:00",
        "iteration": 5,
        "consecutive_rate_limits": "abc",
    })
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = _controller(stub).read_lifecycle_progress()
    # last_shutdown survives the corrupt counter...
    assert result[0] == "2026-04-06T21:00:00+00:00"
    # ...the iteration cursor is untouched...
    assert result[1] == 5
    # ...and the corrupt counter defaults to 0.
    assert result[13] == 0
    # ...and the fail-loud warning names the corrupt field.
    assert "Corrupt consecutive_rate_limits in lifecycle.json" in caplog.text


def test_read_lifecycle_progress_infinite_iteration_preserves_last_shutdown(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``Infinity`` ``iteration`` defaults to 0 without discarding the record.

    Regression for the #430 follow-up: ``json.loads`` parses ``Infinity``
    by default, and ``int(float('inf'))`` raises ``OverflowError`` — a type
    the old ``isinstance(..., int | str)`` guard routed to ``else 0`` but
    the ``int(raw)``-based ``_coerce_lifecycle_int`` first caught only
    ``(ValueError, TypeError)``. ``OverflowError`` escaped both the helper
    and the method's outer ``except``, crashing ``read_lifecycle_progress``
    on exactly the corruption class the per-field guard promised to confine.
    """
    stub = _stub_agent()
    stub.config.lifecycle_path = Path("/fake/inf-iter/lifecycle.json")
    mem_fs["/fake/inf-iter/lifecycle.json"] = json.dumps({
        "last_shutdown": "2026-04-06T21:00:00+00:00",
        "iteration": float("inf"),
        "next_phase": "contemplate",
    })
    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = _controller(stub).read_lifecycle_progress()
    # last_shutdown survives the OverflowError-raising iteration...
    assert result[0] == "2026-04-06T21:00:00+00:00"
    # ...the un-intable cursor defaults to 0...
    assert result[1] == 0
    # ...next_phase survives...
    assert result[2] == "contemplate"
    # ...and a preserved last_shutdown means not crashed.
    assert result[4] is False
    # ...and the fail-loud warning names the corrupt field.
    assert "Corrupt iteration in lifecycle.json" in caplog.text


# --- _format_status_message -----------------------------------------------


def test_format_status_message_unknown_cost() -> None:
    msg = _format_status_message(3, None, "All good")
    assert "<b>Iter 3</b>" in msg
    assert "Cost: unknown" in msg
    assert "All good" in msg
