"""Behavioral guard: one loop phase exercised under the *shipped production*
flag combination.

CI runs the suite with ``MAESTRO_ENV`` unset, and the component ``config``
fixture pins ``env_name=None`` on purpose (#3460) so ambient env cannot leak
into component behavior.  The consequence is a measurement gap: nothing in the
suite runs a loop phase under the combination of levers
``maestro.production.toml`` actually ships.  The production-aware tests in
``tests/test_config_flag_lifecycle.py`` compare values and manifest membership;
they never call a phase.

That is the class of failure a production-only divergence in a phase's priced
call sequence produces: invisible to CI while the loop's gate goes red.  This
module closes it for ``evolve()``, the highest-value phase to pin because its
priced call sequence is lever-dependent (the proposal-139 single-use seam).
Proposal 149 deleted the stage-progression lever and its readiness call, so
the production and base postures no longer differ there.

A new production-only lever that changes a phase's priced call sequence
belongs here.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from maestro import config_settings
from maestro.config import Config
from tests.component.conftest import (
    CONFIG_DIR,
    apply_test_overlay,
    build_base_config,
    make_quality_gates_mock,
    write_self_model,
)
from tests.component.sdk_mock import SDKMock
from tests.support.gap_fuel import fuel_patch

# Levers the production overlay flips *on* (proposal 139 and 125 canaries),
# asserted below to be genuinely part of the base-vs-production diff, so the
# guard degrades loudly rather than passing vacuously if the overlay stops
# flipping them.
_ENABLED_LEVERS_UNDER_TEST = (
    "sdk_single_use_evolution_calls_enabled",
    "host_capacity_sensor_enabled",
)


@pytest.fixture
def production_config(memory_dir, workspace_dir) -> Config:
    """The shipped production ``Config``, with only per-test paths overlaid.

    Shares ``apply_test_overlay`` with the component ``config`` fixture, so the
    only difference from that fixture is the ``production`` TOML overlay
    itself.
    """
    cfg = config_settings.build_config_authoritative(CONFIG_DIR, "production")
    return apply_test_overlay(cfg, memory_dir, workspace_dir)


def test_production_overlay_flips_the_levers_this_module_pins(production_config):
    """Non-vacuity anchor: the levers are really part of the overlay diff.

    Derived from the base-vs-production field diff rather than a hardcoded list
    alone, mirroring ``_PRODUCTION_OVERRIDDEN_LEVERS`` in
    ``test_config_integration``: if the overlay is edited so a lever no longer
    differs, this fails instead of the behavioral guard silently passing for the
    wrong reason.
    """
    base = build_base_config()
    overridden = {
        name
        for name in type(base).__dataclass_fields__
        if getattr(base, name) != getattr(production_config, name)
    }
    for lever in _ENABLED_LEVERS_UNDER_TEST:
        assert lever in overridden, lever
        assert getattr(production_config, lever) is True, lever


@pytest.mark.asyncio
async def test_evolve_under_production_posture_runs_the_single_use_sequence(
    make_agent, production_config, memory_dir, workspace_dir, mem_fs,
):
    """A fuelled ``evolve()`` under the production posture consumes exactly two calls.

    The production response queue is exactly consumed — the propose_plan draft
    and the proposal-150 grounded re-read re-prompt (reflect is deleted,
    proposal 155 step 4) — so an extra priced call desyncs it and fails, and
    every call goes through the single-use seam.
    """
    write_self_model(memory_dir, evolution_count=1,
                     identity_statements=["I am Maestro"])
    # The proposal-150 existence check is rooted at the work-target
    # ``repo_workspace`` (here ``workspace_dir``), not the process CWD, so the
    # cited ``.py`` must exist under that root for the re-read to fire.
    mem_fs[str(workspace_dir / "maestro" / "consciousness.py")] = "x = 1\n"
    agent, _code_host, _vcs = make_agent(config_override=production_config)

    sdk = SDKMock()
    _plan_json = json.dumps({"title": "Production-posture capability",
                             "category": "awareness", "priority": "capability",
                             "rationale": "Measured under the shipped flags",
                             "proposed_changes": ["Improve self-model"],
                             "files_affected": ["maestro/consciousness.py"],
                             "expected_impact": "Deeper self-awareness"})
    # The propose_plan draft, then the proposal-150 grounded re-read
    # re-prompt: the draft cites an existing ``.py`` the mocked tree seam never
    # records as read, so ``propose_plan`` issues exactly one extra single-use
    # call before stamping ``grounded_reads``.
    sdk.then(_plan_json, _plan_json)

    async def _single_use_turn(_session: object, prompt: str, **kwargs: Any) -> Any:
        return await sdk.mock(prompt=prompt, **kwargs)

    # Production runs the evolution lineage through call_policy.single_use_call
    # (proposal 139 canary), so its session seam feeds the same response queue.
    create_session = AsyncMock()
    single_use_seam = {
        "maestro.call_policy.create_session": create_session,
        "maestro.call_policy.query_on_session": _single_use_turn,
        "maestro.call_policy.disconnect_session": AsyncMock(),
    }
    with fuel_patch(), sdk.patch(
        "maestro.consciousness", "maestro.evolution", "maestro.evolution_quality",
        extras=single_use_seam,
    ), \
         patch("maestro.evolution.subprocess.run",
               side_effect=make_quality_gates_mock()):
        await agent.evolve()

    # Exactly the production sequence — propose_plan and its re-read.
    sdk.assert_all_consumed()
    # Every one of those calls went through the single-use seam (not the
    # flag-off bare-query arm).
    callers = [
        c.kwargs["options"].caller for c in create_session.await_args_list
    ]
    assert callers == ["propose_plan", "propose_plan"]
