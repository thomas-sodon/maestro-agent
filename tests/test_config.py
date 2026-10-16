"""Tests for Config, toml_config, and related utilities — mocked filesystem."""

from __future__ import annotations

import ast
import dataclasses
import errno
import inspect
import logging
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

import maestro.config as config_module
import maestro.toml_config as toml_config
from maestro import model_profiles
from maestro.config import Config
from maestro.config_settings import _SECTION_MODELS, build_config_via_pydantic
from maestro.runtime_contract import load_contract
from maestro.toml_config import _deep_merge
from maestro.work_target import WorkTarget

# Load from the committed maestro.toml to prevent drift between test and production defaults.
_BASE_TOML = (Path(__file__).resolve().parent.parent / "maestro.toml").read_text()


def _committed_toml_vals() -> dict[str, Any]:
    """Committed base-``maestro.toml`` scalar values keyed by ``Config`` field name.

    The post-proposal-106 replacement for the retired ``load_toml_layers`` flat
    dict: parses the committed base config and projects each modelled
    ``(section, key)`` onto its ``Config`` field name via ``_SECTION_MODELS``
    (``Field(alias=...)`` or the field name). Raw pre-resolution values (a Path is
    still ``"."``, an empty optional is still ``""``), so the committed-default
    value guards below read it exactly as they read the old loader output.
    """
    data = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "maestro.toml").read_text(
            encoding="utf-8"
        )
    )
    vals: dict[str, Any] = {}
    for section, model_cls in _SECTION_MODELS.items():
        section_data = data.get(section, {})
        for name, field in model_cls.model_fields.items():
            vals[name] = section_data[field.alias or name]
    return vals


# =====================================================================
# Config defaults & derived paths
# =====================================================================


def test_config_defaults() -> None:
    config = Config()
    assert config.dashboard_port == 8420
    assert config.verbose is False
    assert config.telegram_bot_token is None


def test_config_new_fields_defaults() -> None:
    """All operational config fields have correct defaults."""
    config = Config()
    assert config.git_timeout == 30
    assert config.git_quick_timeout == 10
    assert config.selfcheck_timeout == 900
    assert config.implement_timeout == 120
    assert config.pr_creation_timeout == 60
    assert config.gh_timeout == 30
    assert config.shutdown_timeout == 10.0
    assert config.rejection_reason_max == 2000
    assert config.max_page_size == 500
    assert config.log_buffer_size == 500
    assert config.loop_interval == 60
    assert config.loop_evolve_every == 5
    assert config.loop_task_every == 3
    assert config.max_loop_budget_usd_per_hour == 0.0
    assert config.budget_rate_window_hours == 1.0
    assert config.budget_rate_min_cost_usd == 0.10
    assert config.budget_throttle_max_multiplier == 60.0
    assert config.journal_default_count == 10
    assert config.log_tail_lines == 50
    assert config.telegram_max_length == 4096
    assert config.log_dates_shown == 5
    assert config.session_data_days == 10
    assert config.log_max_bytes == 50 * 1024 * 1024
    assert config.log_backup_count == 3
    assert config.structured_transcripts_enabled is True
    assert config.transcript_content_max == 20000
    assert config.cov_fail_under == 97
    assert config.email_address is None
    assert config.email_password is None
    assert config.email_thomas is None
    assert config.email_smtp_host == "smtp.zoho.com"
    assert config.email_smtp_port == 587
    assert config.email_imap_host == "imap.zoho.com"
    assert config.email_imap_port == 993
    assert config.email_max_length == 50000


def test_config_artifacts_dir() -> None:
    config = Config(workspace=Path("/fake/workspace"))
    assert config.artifacts_dir == Path("/fake/workspace/artifacts")


def test_config_log_dir() -> None:
    config = Config(memory_dir=Path("/fake/maestro"))
    assert config.log_dir == Path("/fake/maestro/logs")


def test_config_app_log_path() -> None:
    config = Config(memory_dir=Path("/fake/maestro"))
    assert config.app_log_path == Path("/fake/maestro/logs/maestro.log")


def test_config_sdk_log_dir() -> None:
    config = Config(memory_dir=Path("/fake/maestro"))
    assert config.sdk_log_dir == Path("/fake/maestro/logs/sdk")


def test_config_task_history_path() -> None:
    config = Config(memory_dir=Path("/fake/maestro"))
    assert config.task_history_path == Path("/fake/maestro/task_history.jsonl")


def test_progress_aware_lap_budget_active_both_flags_off() -> None:
    """Proposal 094: with both flags explicitly off ⇒ the progress-aware handoff
    is inactive, so both handoff sites use the gross unlanded-lap count."""
    config = Config(
        progress_aware_lap_budget_enabled=False,
        salvage_before_disengage_enabled=False,
    )
    assert config.progress_aware_lap_budget_active is False


def test_progress_aware_lap_budget_active_dedicated_flag_on() -> None:
    """The dedicated flag activates the progress-aware handoff."""
    config = Config(
        progress_aware_lap_budget_enabled=True,
        salvage_before_disengage_enabled=False,
    )
    assert config.progress_aware_lap_budget_active is True


def test_progress_aware_lap_budget_active_salvage_master_flag_on() -> None:
    """The salvage master flag also activates the progress-aware handoff, so the
    two consuming sites can never drift apart."""
    config = Config(
        progress_aware_lap_budget_enabled=False,
        salvage_before_disengage_enabled=True,
    )
    assert config.progress_aware_lap_budget_active is True


def test_config_ensure_dirs() -> None:
    """Verify ensure_dirs calls mkdir with correct args without touching filesystem."""
    with patch.object(Path, "mkdir") as mock_mkdir:
        config = Config(memory_dir=Path("/fake/maestro_test"))
        config.ensure_dirs()
        assert mock_mkdir.call_count == 2


def test_ensure_dirs_permission_error_warns(caplog: pytest.LogCaptureFixture) -> None:
    """ensure_dirs warns instead of crashing when sdk_log_dir is not writable."""
    config = Config(memory_dir=Path("/fake/maestro_test"))

    def _mkdir_side_effect(self: Path, *args: Any, **kwargs: Any) -> None:
        # Key off the actual directory being created, not a call ordinal, so a
        # future reorder of ensure_dirs() cannot silently exercise the wrong
        # mkdir. autospec=True makes the mock pass the bound Path as ``self``.
        if self == config.sdk_log_dir:
            raise PermissionError("Permission denied")

    with (
        patch.object(Path, "mkdir", autospec=True, side_effect=_mkdir_side_effect),
        caplog.at_level(logging.WARNING, logger="maestro.config"),
    ):
        config.ensure_dirs()  # should not raise

    matches = [
        r for r in caplog.records if "SDK logging unavailable" in r.getMessage()
    ]
    assert len(matches) == 1
    # The warning must name sdk_log_dir — a wrong-target mkdir failure is now
    # loud instead of a silent pass on the wrong directory.
    assert str(config.sdk_log_dir) in matches[0].getMessage()


def test_ensure_dirs_readonly_filesystem_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ensure_dirs warns instead of crashing on a read-only mount (EROFS).

    A read-only bind mount surfaces as a plain ``OSError`` (errno EROFS), not a
    ``PermissionError``, so the handler must catch the wider ``OSError`` to
    degrade SDK logging gracefully instead of crashing startup.
    """
    config = Config(memory_dir=Path("/fake/maestro_test"))
    call_count = 0

    def _mkdir_side_effect(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:  # sdk_log_dir is the second mkdir call
            raise OSError(errno.EROFS, "Read-only file system")

    with (
        patch.object(Path, "mkdir", side_effect=_mkdir_side_effect),
        caplog.at_level(logging.WARNING, logger="maestro.config"),
    ):
        config.ensure_dirs()  # should not raise

    matches = [
        r for r in caplog.records if "SDK logging unavailable" in r.getMessage()
    ]
    assert len(matches) == 1


def test_dataclass_defaults_match_toml() -> None:
    """Config() defaults must match committed maestro.toml values.

    Path and string-to-optional fields are compared with special handling
    because TOML stores raw strings while Config stores resolved types.
    """
    toml_vals = _committed_toml_vals()
    config = Config()
    # Fields that need special comparison (TOML raw string vs Config resolved type)
    _skip_fields = {
        "workspace",
        "memory_dir",
        "gate_lease_slot_dir",
        "github_repo",
        "github_reviewer",
        # Deliberately on in the committed TOML (the measured harm is on the
        # host-shared slot directory) while the dataclass keeps today's behaviour.
        "gate_lease_admission_cap_enabled",
    }
    for key, value in toml_vals.items():
        if key in _skip_fields:
            continue
        assert getattr(config, key) == value, f"{key}: {getattr(config, key)} != {value}"
    # Verify the skipped fields exist in TOML
    assert toml_vals["workspace"] == "."
    assert toml_vals["memory_dir"] == "data"
    assert toml_vals["github_repo"] == ""
    assert toml_vals["github_reviewer"] == ""
    assert toml_vals["gate_lease_admission_cap_enabled"] is True
    assert config.gate_lease_admission_cap_enabled is False


# =====================================================================
# Config.validate
# =====================================================================


def test_config_valid_passes() -> None:
    """Default config should pass validation."""
    Config().validate()


def test_config_accepts_zero_checkpoint_max_age_days() -> None:
    """Zero disables the stale-checkpoint sweep — a valid configuration."""
    Config(checkpoint_max_age_days=0).validate()


# ---------------------------------------------------------------------
# SDK provider selection (proposal 058 Step 6)
# ---------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["anthropic", "openai", "cli_paced"])
def test_config_accepts_valid_sdk_provider(provider: str) -> None:
    Config(sdk_provider=provider).validate()


def test_config_accepts_valid_phase_provider() -> None:
    Config(sdk_phase_providers={"artifact_review": "openai"}).validate()


def test_config_rejects_unknown_phase_name() -> None:
    config = Config(sdk_phase_providers={"not_a_phase": "anthropic"})
    with pytest.raises(ValueError, match="unknown phase 'not_a_phase'"):
        config.validate()


def test_config_rejects_unknown_phase_provider_value() -> None:
    config = Config(sdk_phase_providers={"artifact_review": "bogus"})
    with pytest.raises(
        ValueError, match=r"sdk_phase_providers\['artifact_review'\] must be one of",
    ):
        config.validate()


def test_config_rejects_phase_providers_without_session_reuse() -> None:
    """Per-phase routing lives at the session layer (058 Step 6 runtime), so an
    override with session reuse off could never take effect — fail loud rather
    than silently ignore it."""
    config = Config(
        sdk_phase_providers={"contemplation": "cli_paced"},
        enable_session_reuse=False,
    )
    with pytest.raises(
        ValueError, match="require enable_session_reuse=true",
    ):
        config.validate()


def test_config_accepts_phase_providers_with_session_reuse() -> None:
    """The default ``enable_session_reuse=True`` lets a per-phase override route."""
    Config(
        sdk_phase_providers={"contemplation": "cli_paced"},
        enable_session_reuse=True,
    ).validate()


def test_config_no_phase_providers_allows_session_reuse_off() -> None:
    """Disabling session reuse is fine when no per-phase override is configured."""
    Config(sdk_phase_providers={}, enable_session_reuse=False).validate()


# ---------------------------------------------------------------------
# Work target (proposal 107 Step 1) — framing validation + resolver
# ---------------------------------------------------------------------


@pytest.mark.parametrize("framing", ["self_evolution", "external_delivery"])
def test_config_accepts_valid_work_framings(framing: str) -> None:
    Config(work_focus_profile=framing, work_task_framing=framing).validate()


def test_work_target_defaults_to_self() -> None:
    """With no [work] override the resolved target mirrors the self scalars."""
    config = Config(workspace=Path("/self/repo"), github_repo="self/maestro")
    target = config.work_target()
    assert isinstance(target, WorkTarget)
    assert target.repo_workspace == Path("/self/repo")
    assert target.github_repo == "self/maestro"
    assert target.gate_source_path == "maestro/"
    assert target.gate_tests_path == "tests/"
    assert target.gate_lint_extra_paths == ("scripts/",)
    assert target.task_framing == "self_evolution"
    assert target.focus_profile == "self_evolution"


def test_work_target_uses_external_overrides() -> None:
    """Non-empty [work] overrides point the target at the external values."""
    config = Config(
        workspace=Path("/self/repo"),
        github_repo="self/maestro",
        work_repo_workspace="/external/repo",
        work_github_repo="customer/app",
        work_task_framing="external_delivery",
        work_focus_profile="external_delivery",
    )
    target = config.work_target()
    assert target.repo_workspace == Path("/external/repo")
    assert target.github_repo == "customer/app"
    assert target.task_framing == "external_delivery"
    assert target.focus_profile == "external_delivery"


@patch("dotenv.load_dotenv")
def test_config_from_env_work_defaults_to_self(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """The committed [work] defaults resolve to a self-pointed WorkTarget."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    target = config.work_target()
    assert target.repo_workspace == config.workspace
    assert target.github_repo == config.github_repo
    assert target.gate_source_path == "maestro/"
    assert target.gate_tests_path == "tests/"
    assert target.gate_lint_extra_paths == ("scripts/",)
    assert target.task_framing == "self_evolution"
    assert target.focus_profile == "self_evolution"


@patch("dotenv.load_dotenv")
def test_config_from_env_work_external_overlay(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [work] overlay round-trips through from_env to an external target."""
    toml = (
        _BASE_TOML
        .replace('repo_workspace = ""', 'repo_workspace = "/external/repo"')
        .replace('github_repo = ""', 'github_repo = "customer/app"')
        .replace(
            'focus_profile = "self_evolution"',
            'focus_profile = "external_delivery"',
        )
        .replace(
            'task_framing = "self_evolution"',
            'task_framing = "external_delivery"',
        )
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    target = config.work_target()
    assert target.repo_workspace == Path("/external/repo")
    assert target.github_repo == "customer/app"
    assert target.task_framing == "external_delivery"
    assert target.focus_profile == "external_delivery"


@patch("dotenv.load_dotenv")
def test_config_from_env_work_empty_lint_extra_paths(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """An empty ``[work] gate_lint_extra_paths`` list resolves to no extra atoms."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        'gate_lint_extra_paths = ["scripts/"]', "gate_lint_extra_paths = []",
    )
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.work_target().gate_lint_extra_paths == ()


@patch("dotenv.load_dotenv")
def test_config_from_env_refuses_retired_net_burn_objective_key(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """The retired ``[work] net_burn_objective_enabled`` key fails loud at load.

    Proposal 155 Step 3 deleted the planning-objective facet; ``extra="forbid"``
    turns a stale overlay that still sets it into a startup error.
    """
    assert "net_burn_objective_enabled" not in _BASE_TOML
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "[work]\n", "[work]\nnet_burn_objective_enabled = true\n", 1,
    )
    with pytest.raises(ValueError, match="net_burn_objective_enabled"):
        Config.from_env(config_dir=Path("/fake/toml"))


def test_resolved_sdk_provider_default_is_anthropic() -> None:
    assert Config().resolved_sdk_provider() == "anthropic"


def test_resolved_sdk_provider_returns_explicit_provider() -> None:
    """The proposal-056 ``[sdk] backend`` alias was retired (056 Step 11), so the
    resolver returns ``sdk_provider`` verbatim."""
    assert Config(sdk_provider="openai").resolved_sdk_provider() == "openai"


def test_provider_for_phase_returns_override() -> None:
    config = Config(
        sdk_provider="anthropic",
        sdk_phase_providers={"artifact_review": "openai"},
    )
    assert config.provider_for_phase("artifact_review") == "openai"


def test_provider_for_phase_falls_back_to_global() -> None:
    config = Config(sdk_provider="openai")
    assert config.provider_for_phase("contemplation") == "openai"


# ---------------------------------------------------------------------
# cli_paced provider Cadence + stream-idle overrides (proposal 056 Step 12)
# ---------------------------------------------------------------------


def test_config_cli_paced_defaults_are_empty() -> None:
    """Absent ``[sdk.cli_paced]`` leaves the provider on maestro_sdk defaults."""
    config = Config()
    assert config.sdk_cli_paced_cadence == {}
    assert config.sdk_cli_paced_stream_idle_timeout_seconds == 0.0


def test_config_accepts_valid_cli_paced_overrides() -> None:
    Config(
        sdk_cli_paced_cadence={"mean_key_ms": 50.0, "cols": 80},
        sdk_cli_paced_stream_idle_timeout_seconds=120.0,
    ).validate()


@pytest.mark.parametrize("key", ["cols", "rows"])
def test_config_rejects_fractional_cli_paced_int_cadence_key(key: str) -> None:
    """``cols``/``rows`` are ``int`` Cadence fields — a fractional value is
    rejected at validate(), not later when it reaches the pty winsize."""
    config = Config(sdk_cli_paced_cadence={key: 80.5})
    with pytest.raises(
        ValueError,
        match=rf"sdk_cli_paced_cadence\['{key}'\] must be a whole number",
    ):
        config.validate()


def test_config_accepts_integral_float_cli_paced_int_cadence_key() -> None:
    """An integral float (``80.0``) for ``cols``/``rows`` is accepted — only the
    fractional part is rejected."""
    Config(sdk_cli_paced_cadence={"cols": 80.0, "rows": 24.0}).validate()


def test_config_rejects_unknown_cli_paced_cadence_key() -> None:
    config = Config(sdk_cli_paced_cadence={"not_a_knob": 1.0})
    with pytest.raises(ValueError, match="unknown key 'not_a_knob'"):
        config.validate()


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True])
def test_config_rejects_invalid_cli_paced_cadence_value(bad: float) -> None:
    """Non-finite *and* negative cadence knobs are rejected at validate() — a
    negative ``min_key_ms`` would otherwise reach ``asyncio.sleep`` and raise."""
    config = Config(sdk_cli_paced_cadence={"mean_key_ms": bad})
    with pytest.raises(
        ValueError,
        match=r"sdk_cli_paced_cadence\['mean_key_ms'\] must be a finite",
    ):
        config.validate()


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True])
def test_config_rejects_invalid_cli_paced_stream_idle(bad: float) -> None:
    config = Config(sdk_cli_paced_stream_idle_timeout_seconds=bad)
    with pytest.raises(
        ValueError, match="sdk_cli_paced_stream_idle_timeout_seconds must be",
    ):
        config.validate()


@pytest.mark.parametrize("key", ["thinking_pause_prob", "size_jitter"])
def test_config_rejects_above_one_cli_paced_unit_interval_key(key: str) -> None:
    """``thinking_pause_prob``/``size_jitter`` are [0, 1] knobs — a value > 1 is
    rejected at validate() (it would distort pacing, not error in maestro_sdk)."""
    config = Config(sdk_cli_paced_cadence={key: 2.0})
    with pytest.raises(
        ValueError,
        match=rf"sdk_cli_paced_cadence\['{key}'\] must be in \[0, 1\]",
    ):
        config.validate()


@pytest.mark.parametrize("key", ["thinking_pause_prob", "size_jitter"])
@pytest.mark.parametrize("ok", [0.0, 0.5, 1.0])
def test_config_accepts_in_range_cli_paced_unit_interval_key(
    key: str, ok: float
) -> None:
    """The interval bounds (0.0 and 1.0) and an interior value are accepted."""
    Config(sdk_cli_paced_cadence={key: ok}).validate()


def test_cli_paced_cadence_keys_are_real_cadence_fields() -> None:
    """``VALID_CLI_PACED_CADENCE_KEYS`` must stay ⊆ ``maestro_sdk.Cadence``'s
    fields so the hardcoded whitelist (kept out of config to avoid importing
    maestro_sdk on the validate path) cannot drift from the real knobs."""
    from dataclasses import fields as dc_fields

    from maestro.config import VALID_CLI_PACED_CADENCE_KEYS
    from maestro_sdk import Cadence

    cadence_field_names = {f.name for f in dc_fields(Cadence)}
    assert cadence_field_names >= VALID_CLI_PACED_CADENCE_KEYS


def test_int_cli_paced_cadence_keys_are_int_cadence_fields() -> None:
    """``INT_CLI_PACED_CADENCE_KEYS`` must name the ``int``-typed ``Cadence``
    fields (and stay ⊆ the valid set) so the whole-number gate in ``validate()``
    cannot drift from the real field types."""
    from dataclasses import fields as dc_fields

    from maestro.config import (
        INT_CLI_PACED_CADENCE_KEYS,
        VALID_CLI_PACED_CADENCE_KEYS,
    )
    from maestro_sdk import Cadence

    assert INT_CLI_PACED_CADENCE_KEYS <= VALID_CLI_PACED_CADENCE_KEYS
    # ``f.type`` is the bare annotation: a string under ``from __future__ import
    # annotations`` (``"int"``), the real type otherwise — accept either form.
    int_field_names = {
        f.name for f in dc_fields(Cadence) if f.type in ("int", int)
    }
    assert int_field_names == INT_CLI_PACED_CADENCE_KEYS


def test_unit_interval_cli_paced_cadence_keys_are_valid_keys() -> None:
    """``UNIT_INTERVAL_CLI_PACED_CADENCE_KEYS`` must stay ⊆ the valid set so the
    [0, 1] gate in ``validate()`` cannot name a knob the schema doesn't accept."""
    from maestro.config import (
        UNIT_INTERVAL_CLI_PACED_CADENCE_KEYS,
        VALID_CLI_PACED_CADENCE_KEYS,
    )

    assert UNIT_INTERVAL_CLI_PACED_CADENCE_KEYS <= VALID_CLI_PACED_CADENCE_KEYS


# ---------------------------------------------------------------------
# cli_paced slash-command metrics probe knobs (proposal 062 Step 6)
# ---------------------------------------------------------------------


def test_config_probe_metrics_defaults_keep_probing_off() -> None:
    """The shipped defaults leave the probe disabled at every-turn cadence."""
    config = Config()
    assert config.sdk_cli_paced_probe_metrics_enabled is False
    assert config.sdk_cli_paced_probe_metrics_every == 1
    config.validate()


def test_config_accepts_valid_probe_metrics_knobs() -> None:
    Config(
        sdk_cli_paced_probe_metrics_enabled=True,
        sdk_cli_paced_probe_metrics_every=5,
    ).validate()


@pytest.mark.parametrize("bad", [1, 0, "true", None])
def test_config_rejects_non_bool_probe_metrics_enabled(bad: object) -> None:
    config = Config(sdk_cli_paced_probe_metrics_enabled=bad)  # type: ignore[arg-type]
    with pytest.raises(
        ValueError, match="sdk_cli_paced_probe_metrics_enabled must be a bool",
    ):
        config.validate()


def test_config_merge_truth_ttl_seconds_default() -> None:
    """Proposal 087's merge-truth freshness TTL ships at a conservative 300s."""
    config = Config()
    assert config.merge_truth_ttl_seconds == 300
    config.validate()


def test_config_merge_truth_ttl_seconds_zero_is_valid() -> None:
    """A zero TTL (always re-observe) is a valid, non-negative configuration."""
    Config(merge_truth_ttl_seconds=0).validate()


def test_config_prestamp_merge_truth_gate_default_off() -> None:
    """Proposal 087's pre-stamp merge-truth gate ships disabled."""
    config = Config()
    assert config.prestamp_merge_truth_gate_enabled is False
    config.validate()


def test_config_mechanical_outcome_authority_key_retired(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 105 retired the ``[loop] mechanical_outcome_authority_enabled``
    flag: the git-grounded authority (``outcome_authority.decide_outcome``) is now
    the unconditional author of the remediation landing verdict. The field is gone
    from ``Config`` and the key is no longer a recognized ``[loop]`` knob — a stray
    occurrence is now rejected by the declarative parser's ``extra="forbid"`` guard,
    never round-tripping through the spec."""
    assert not hasattr(Config(), "mechanical_outcome_authority_enabled")
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "[loop]\n",
        "[loop]\nmechanical_outcome_authority_enabled = true\n",
        1,
    )
    with pytest.raises(ValidationError):
        build_config_via_pydantic(Path("/fake/toml"))


def test_config_phantom_remediation_keys_retired(
    mem_fs: dict[str, str],
) -> None:
    """The redundant phantom-remediation audit rule (rule 10) was retired: its
    landing verdict is git-grounded at record time and settled PRs are dequeued
    mechanically, so the rule caught nothing. Both ``[audit]`` flags are gone
    from ``Config`` and neither key round-trips through the spec — a stray
    occurrence is ignored, not accepted. The still-present
    ``settlement_reconciler_enabled`` control proves the ``hasattr`` probe is not
    vacuously true (the stale-field-drop idiom)."""
    config = Config()
    # Control: a sibling [audit] flag that stays, so the negative checks below
    # cannot pass merely because ``hasattr`` is broken on this instance.
    assert hasattr(config, "settlement_reconciler_enabled")
    assert not hasattr(config, "phantom_remediation_enabled")
    assert not hasattr(config, "phantom_remediation_ready_only_enabled")
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "[audit]\n",
        "[audit]\nphantom_remediation_enabled = true\n"
        "phantom_remediation_ready_only_enabled = true\n",
        1,
    )
    with pytest.raises(ValidationError):
        build_config_via_pydantic(Path("/fake/toml"))


def test_config_observed_ready_standdown_default_on() -> None:
    """Proposal 089's observed ready-and-waiting gate ships enabled by default.

    Review prescribed flipping this one flag to end the ready-and-waiting
    re-service treadmill; the field default agrees with ``maestro.toml``.
    """
    config = Config()
    assert config.observed_ready_standdown_enabled is True
    config.validate()


def test_config_account_ui_v2_default_on() -> None:
    """Proposal 122's account-dashboard navigation shell ships on by default.

    Graduated 2026-09-03 from the default-off dark launch: an operator loading
    ``/account`` gets the grouped, ARIA-correct console with no config edit.
    Setting the flag false is the burn-in-window rollback to the flat pill nav,
    exercised by
    ``tests/test_dashboard.py::test_account_flag_off_rollback_serves_template_byte_identical``.
    """
    config = Config()
    assert config.account_ui_v2 is True
    config.validate()


def test_config_tend_main_merge_default_on() -> None:
    """Proposal 128's merge-queue cutover kill-switch defaults on.

    Default on preserves today's tend behaviour byte-for-byte (the phase still
    merges ``origin/main`` into a behind PR and pushes). Flipping it false is the
    one-line cutover lever — and the reversible kill-switch — for the GitHub merge
    queue, exercised by
    ``tests/test_evolution_tend.py::test_tend_main_merge_off_cleanly_behind_is_noop``.
    """
    config = Config()
    assert config.tend_main_merge_enabled is True
    config.validate()


def test_config_frame_discharge_bind_default_off() -> None:
    """Proposal 112's frame-discharge costume cap ships disabled and inert."""
    config = Config()
    assert config.frame_discharge_bind_enabled is False
    config.validate()


def test_config_salvage_before_disengage_default_on() -> None:
    """Proposal 094's salvage-before-disengage gate ships enabled by default."""
    config = Config()
    assert config.salvage_before_disengage_enabled is True
    config.validate()


def test_config_wip_primacy_default_on() -> None:
    """Proposal 092's WIP-primacy gate ships enabled by default."""
    config = Config()
    assert config.wip_primacy_enabled is True
    config.validate()


def test_config_task_branch_reuse_default_on() -> None:
    """Proposal 116's task-branch-reuse master switch ships enabled by default."""
    config = Config()
    assert config.task_branch_reuse_enabled is True
    config.validate()


def test_config_task_branch_reuse_committed_toml_default_on() -> None:
    """The committed maestro.toml pins task_branch_reuse_enabled on."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["task_branch_reuse_enabled"] is True


@patch("dotenv.load_dotenv")
def test_config_from_env_task_branch_reuse_overlay_false(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting task_branch_reuse_enabled False parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.task_branch_reuse_enabled is True

    toml = _BASE_TOML.replace(
        "task_branch_reuse_enabled = true",
        "task_branch_reuse_enabled = false",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.task_branch_reuse_enabled is False


def test_config_self_directed_tasks_default_on() -> None:
    """Proposal 116's self-directed-pipeline master switch ships enabled."""
    config = Config()
    assert config.self_directed_tasks_enabled is True
    config.validate()


def test_config_self_directed_tasks_committed_toml_default_on() -> None:
    """The committed maestro.toml pins self_directed_tasks_enabled on."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["self_directed_tasks_enabled"] is True


@patch("dotenv.load_dotenv")
def test_config_from_env_self_directed_tasks_overlay_false(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting self_directed_tasks_enabled False parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.self_directed_tasks_enabled is True

    toml = _BASE_TOML.replace(
        "self_directed_tasks_enabled = true",
        "self_directed_tasks_enabled = false",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.self_directed_tasks_enabled is False


def test_config_restatement_concept_key_default_off() -> None:
    """Proposal 101's concept-key restatement matching ships disabled."""
    config = Config()
    assert config.loop_restatement_concept_key_enabled is False
    config.validate()


def test_config_restatement_concept_key_committed_toml_default_off() -> None:
    """The committed maestro.toml pins restatement_concept_key_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["loop_restatement_concept_key_enabled"] is False


def test_config_session_landing_default_off() -> None:
    """The post-session mechanical landing actuator ships disabled by default."""
    config = Config()
    assert config.session_landing_enabled is False
    config.validate()


def test_config_session_landing_committed_toml_default_off() -> None:
    """The committed maestro.toml pins session_landing_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["session_landing_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_session_landing_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting session_landing_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.session_landing_enabled is False

    toml = _BASE_TOML.replace(
        "session_landing_enabled = false", "session_landing_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.session_landing_enabled is True


def test_config_grounded_reflection_default_off() -> None:
    """Proposal 132's grounded-contemplation switch ships disabled by default."""
    config = Config()
    assert config.grounded_reflection_enabled is False
    config.validate()


def test_config_grounded_reflection_committed_toml_default_off() -> None:
    """The committed maestro.toml pins grounded_reflection_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["grounded_reflection_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_grounded_reflection_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting grounded_reflection_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.grounded_reflection_enabled is False

    toml = _BASE_TOML.replace(
        "grounded_reflection_enabled = false", "grounded_reflection_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.grounded_reflection_enabled is True


def test_config_cooldown_signature_keying_default_off() -> None:
    """Proposal 137's cooldown signature keying ships disabled by default."""
    config = Config()
    assert config.cooldown_signature_keying_enabled is False
    config.validate()


def test_config_cooldown_signature_keying_committed_toml_default_off() -> None:
    """The committed maestro.toml pins cooldown_signature_keying_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["cooldown_signature_keying_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_cooldown_signature_keying_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting cooldown_signature_keying_enabled True parses."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.cooldown_signature_keying_enabled is False

    toml = _BASE_TOML.replace(
        "cooldown_signature_keying_enabled = false",
        "cooldown_signature_keying_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.cooldown_signature_keying_enabled is True


def test_config_owed_act_binding_default_off() -> None:
    """Proposal 137's owed-act binding ships disabled by default."""
    config = Config()
    assert config.owed_act_binding_enabled is False
    config.validate()


def test_config_owed_act_binding_committed_toml_default_off() -> None:
    """The committed maestro.toml pins owed_act_binding_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["owed_act_binding_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_owed_act_binding_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting owed_act_binding_enabled True parses."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.owed_act_binding_enabled is False

    toml = _BASE_TOML.replace(
        "owed_act_binding_enabled = false",
        "owed_act_binding_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.owed_act_binding_enabled is True


def test_config_cooldown_backoff_base_default() -> None:
    """Proposal 137's backoff ladder starts at a one-hour first rung."""
    config = Config()
    assert config.cooldown_backoff_base_hours == 1.0
    config.validate()


def test_config_cooldown_backoff_base_committed_toml_value() -> None:
    """The committed maestro.toml pins the backoff base at 1h."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["cooldown_backoff_base_hours"] == 1.0


@patch("dotenv.load_dotenv")
def test_config_from_env_cooldown_backoff_base_overlay(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay can retune the backoff base."""
    toml = _BASE_TOML.replace(
        "cooldown_backoff_base_hours = 1.0",
        "cooldown_backoff_base_hours = 3.5",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.cooldown_backoff_base_hours == 3.5


@patch("dotenv.load_dotenv")
def test_config_from_env_cooldown_backoff_base_rejects_non_positive(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A zero or negative base is refused: the ladder has no meaningful rung."""
    toml = _BASE_TOML.replace(
        "cooldown_backoff_base_hours = 1.0",
        "cooldown_backoff_base_hours = 0.0",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    with pytest.raises(ValueError, match="cooldown_backoff_base_hours"):
        Config.from_env(config_dir=Path("/fake/toml"))


def test_config_gate_lease_cgroup_reserve_default_off() -> None:
    """Proposal 134's bound-aware reserve switch ships disabled by default."""
    config = Config()
    assert config.gate_lease_cgroup_reserve_enabled is False
    config.validate()


def test_config_gate_lease_cgroup_reserve_committed_toml_default_off() -> None:
    """The committed maestro.toml pins gate_lease_cgroup_reserve_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["gate_lease_cgroup_reserve_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_gate_lease_cgroup_reserve_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting gate_lease_cgroup_reserve_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_lease_cgroup_reserve_enabled is False

    toml = _BASE_TOML.replace(
        "gate_lease_cgroup_reserve_enabled = false",
        "gate_lease_cgroup_reserve_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_lease_cgroup_reserve_enabled is True


def test_config_gate_scope_impacted_default_off() -> None:
    """Proposal 134's impacted-scope fix-loop switch ships disabled by default."""
    config = Config()
    assert config.gate_scope_impacted_enabled is False
    config.validate()


def test_config_gate_scope_impacted_committed_toml_default_off() -> None:
    """The committed maestro.toml pins gate_scope_impacted_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["gate_scope_impacted_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_gate_scope_impacted_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting gate_scope_impacted_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_scope_impacted_enabled is False

    toml = _BASE_TOML.replace(
        "gate_scope_impacted_enabled = false",
        "gate_scope_impacted_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_scope_impacted_enabled is True


def test_config_gate_environment_oracle_default_off() -> None:
    """Proposal 133's pre-spend runnability consult ships disabled by default."""
    config = Config()
    assert config.gate_environment_oracle_enabled is False
    config.validate()


def test_config_gate_environment_oracle_committed_toml_default_off() -> None:
    """The committed maestro.toml pins gate_environment_oracle_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["gate_environment_oracle_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_gate_environment_oracle_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting gate_environment_oracle_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_environment_oracle_enabled is False

    toml = _BASE_TOML.replace(
        "gate_environment_oracle_enabled = false",
        "gate_environment_oracle_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_environment_oracle_enabled is True


def test_config_attempt_disposition_default_off() -> None:
    """Proposal 131's attempt-disposition oracle switch ships disabled by default."""
    config = Config()
    assert config.attempt_disposition_enabled is False
    config.validate()


def test_config_attempt_disposition_committed_toml_default_off() -> None:
    """The committed maestro.toml pins attempt_disposition_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["attempt_disposition_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_attempt_disposition_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting attempt_disposition_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.attempt_disposition_enabled is False

    toml = _BASE_TOML.replace(
        "attempt_disposition_enabled = false", "attempt_disposition_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.attempt_disposition_enabled is True


def test_config_gate_observation_default_off() -> None:
    """Proposal 124's gate observation ships disabled — flag-off is byte-identical."""
    config = Config()
    assert config.gate_observation_enabled is False
    config.validate()


def test_config_gate_observation_committed_toml_default_off() -> None:
    """The committed maestro.toml pins gate_observation_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["gate_observation_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_gate_observation_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting gate_observation_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_observation_enabled is False

    toml = _BASE_TOML.replace(
        "gate_observation_enabled = false",
        "gate_observation_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_observation_enabled is True


def test_config_conscious_workspace_observe_default_off() -> None:
    """Proposal 149 stops the zero-citation workspace observer: off in base."""
    config = Config()
    assert config.conscious_workspace_observe_enabled is False
    config.validate()


def test_config_conscious_workspace_observe_committed_toml_off() -> None:
    """The committed maestro.toml turns conscious_workspace_observe_enabled off
    in base (proposal 149)."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["conscious_workspace_observe_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_conscious_workspace_observe_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] setting conscious_workspace_observe_enabled True (restoring the burn-in) parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.conscious_workspace_observe_enabled is False

    toml = _BASE_TOML.replace(
        "conscious_workspace_observe_enabled = false",
        "conscious_workspace_observe_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.conscious_workspace_observe_enabled is True


def test_config_conscious_workspace_broadcast_default_off() -> None:
    """Proposal 130's conscious-workspace broadcast ships disabled — flag-off is
    byte-identical (introspect receives an empty block)."""
    config = Config()
    assert config.conscious_workspace_broadcast_enabled is False
    config.validate()


def test_config_conscious_workspace_broadcast_committed_toml_default_off() -> None:
    """The committed maestro.toml pins conscious_workspace_broadcast_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["conscious_workspace_broadcast_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_conscious_workspace_broadcast_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting conscious_workspace_broadcast_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.conscious_workspace_broadcast_enabled is False

    toml = _BASE_TOML.replace(
        "conscious_workspace_broadcast_enabled = false",
        "conscious_workspace_broadcast_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.conscious_workspace_broadcast_enabled is True


def test_config_host_capacity_sensor_default_off() -> None:
    """Proposal 125's host-capacity sensor ships disabled — flag-off is silent."""
    config = Config()
    assert config.host_capacity_sensor_enabled is False
    config.validate()


def test_config_host_capacity_sensor_committed_toml_default_off() -> None:
    """The committed maestro.toml pins host_capacity_sensor_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["host_capacity_sensor_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_host_capacity_sensor_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay setting host_capacity_sensor_enabled True parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.host_capacity_sensor_enabled is False

    toml = _BASE_TOML.replace(
        "host_capacity_sensor_enabled = false",
        "host_capacity_sensor_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.host_capacity_sensor_enabled is True


def test_config_sqlite_state_default_off() -> None:
    """The SQLite state store ships disabled by default (proposal 106)."""
    config = Config()
    assert config.sqlite_state_enabled is False
    config.validate()


def test_config_sqlite_state_committed_toml_default_off() -> None:
    """The committed maestro.toml pins sqlite_state_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["sqlite_state_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_sqlite_state_parses_from_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """[paths] sqlite_state_enabled parses through ``Config.from_env``."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.sqlite_state_enabled is False

    toml = _BASE_TOML.replace(
        "sqlite_state_enabled = false", "sqlite_state_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.sqlite_state_enabled is True


@pytest.mark.parametrize(
    "old, new, field_name, expected",
    [
        (
            "duplicate_pending_plan_threshold = 0.5",
            "duplicate_pending_plan_threshold = 0.9",
            "loop_duplicate_pending_plan_threshold",
            0.9,
        ),
        (
            "pending_plan_cluster_threshold = 2",
            "pending_plan_cluster_threshold = 7",
            "loop_pending_plan_cluster_threshold",
            7,
        ),
        (
            "pending_cluster_threshold = 0.35",
            "pending_cluster_threshold = 0.99",
            "loop_pending_cluster_threshold",
            0.99,
        ),
        (
            "pending_cluster_max = 2",
            "pending_cluster_max = 9",
            "loop_pending_cluster_max",
            9,
        ),
        (
            "pending_cluster_sweep_enabled = true",
            "pending_cluster_sweep_enabled = false",
            "loop_pending_cluster_sweep_enabled",
            False,
        ),
        (
            "restatement_sweep_enabled = true",
            "restatement_sweep_enabled = false",
            "loop_restatement_sweep_enabled",
            False,
        ),
        (
            "restatement_concept_key_enabled = false",
            "restatement_concept_key_enabled = true",
            "loop_restatement_concept_key_enabled",
            True,
        ),
        (
            "pending_plan_submit_block_threshold = 5",
            "pending_plan_submit_block_threshold = 11",
            "loop_pending_plan_submit_block_threshold",
            11,
        ),
        (
            "quiet_skip_enabled = true",
            "quiet_skip_enabled = false",
            "quiet_skip_enabled",
            False,
        ),
        (
            "executor_loop_detect_repeats = 3",
            "executor_loop_detect_repeats = 9",
            "executor_loop_detect_repeats",
            9,
        ),
        (
            "plan_review_expiry_days = 21",
            "plan_review_expiry_days = 45",
            "loop_plan_review_expiry_days",
            45,
        ),
        (
            "warning_hold_threshold = 3",
            "warning_hold_threshold = 8",
            "sdk_warning_hold_threshold",
            8,
        ),
        (
            "warning_hold_probe_interval = 60",
            "warning_hold_probe_interval = 120",
            "sdk_warning_hold_probe_interval",
            120,
        ),
    ],
)
def test_config_from_env_honours_formerly_unwired_key(
    mem_fs: dict[str, str],
    old: str,
    new: str,
    field_name: str,
    expected: object,
) -> None:
    """The thirteen formerly-unwired [loop]/[sdk] keys now take effect via from_env.

    Before proposal 106, ``Config.from_env`` read each into ``toml_vals`` and
    dropped it, so the dataclass default always won regardless of the TOML value.
    Now the constructor consumes them, so a non-default overlay reaches the built
    ``Config``. Loading the committed base first proves the default still holds
    when the TOML is unchanged (behaviour-preserving for the shipped config).
    """
    # The overlay value is genuinely non-default, so honouring it is observable.
    default_value = getattr(Config(), field_name)
    assert default_value != expected
    # A precise single-line substring so no unintended line is rewritten.
    assert _BASE_TOML.count(old) == 1

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert getattr(config, field_name) == default_value

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(old, new)
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert getattr(config, field_name) == expected


def test_config_gate_convergence_ceiling_default() -> None:
    """Gate convergence (proposal 110) ships with a ceiling of 20."""
    config = Config()
    assert config.gate_convergence_ceiling == 20
    config.validate()


def test_config_gate_convergence_committed_toml_ceiling() -> None:
    """The committed maestro.toml pins the convergence ceiling at 20."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["gate_convergence_ceiling"] == 20


@patch("dotenv.load_dotenv")
def test_config_from_env_gate_convergence_overlay(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] overlay changing the convergence ceiling parses through."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_convergence_ceiling == 20

    toml = _BASE_TOML.replace(
        "gate_convergence_ceiling = 20", "gate_convergence_ceiling = 5",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.gate_convergence_ceiling == 5


def test_config_gate_convergence_enabled_key_retired(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 110 retired the ``[loop] gate_convergence_enabled`` flag: the
    implement-phase gate loop now routes unconditionally through
    ``converge_to_green``. The field is gone from ``Config`` and the key is no
    longer a recognized ``[loop]`` knob — a stray occurrence is now rejected by the
    declarative parser's ``extra="forbid"`` guard, never round-tripping through the
    spec. The still-present ``gate_convergence_ceiling`` control proves the
    ``hasattr`` probe is not vacuously true (the stale-field-drop idiom)."""
    config = Config()
    # Control: the surviving sibling field, so the negative check below cannot
    # pass merely because ``hasattr`` is broken on this instance.
    assert hasattr(config, "gate_convergence_ceiling")
    assert not hasattr(config, "gate_convergence_enabled")
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "[loop]\n",
        "[loop]\ngate_convergence_enabled = true\n",
        1,
    )
    with pytest.raises(ValidationError):
        build_config_via_pydantic(Path("/fake/toml"))


def test_config_structured_transcripts_committed_toml_default_on() -> None:
    """The committed maestro.toml pins structured_transcripts_enabled on."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["structured_transcripts_enabled"] is True


@patch("dotenv.load_dotenv")
def test_config_from_env_structured_transcripts_overlay_false(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [logs] overlay flipping structured_transcripts_enabled False parses through.

    Exercises the full round-trip: ``maestro.toml`` value → the declarative section
    model → ``Config``. The committed default parses as ``True``; an overlay that
    flips the raw ``true`` to ``false`` reaches ``Config`` as ``False``.
    """
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.structured_transcripts_enabled is True

    toml = _BASE_TOML.replace(
        "structured_transcripts_enabled = true",
        "structured_transcripts_enabled = false",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.structured_transcripts_enabled is False


def test_config_transcript_content_max_committed_toml_default() -> None:
    """The committed maestro.toml pins transcript_content_max at 20000."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["transcript_content_max"] == 20000


@patch("dotenv.load_dotenv")
def test_config_from_env_transcript_content_max_overlay(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [logs] overlay setting transcript_content_max parses through to Config."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.transcript_content_max == 20000

    toml = _BASE_TOML.replace(
        "transcript_content_max = 20000",
        "transcript_content_max = 500",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.transcript_content_max == 500


def test_config_owned_act_preempt_default_off() -> None:
    """The owned-act slot preemption ships disabled (safe/experimental)."""
    config = Config()
    assert config.owned_act_preempt_enabled is False
    config.validate()


def test_config_owned_act_preempt_committed_toml_default_off() -> None:
    """The committed maestro.toml pins owned_act_preempt_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["owned_act_preempt_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_owned_act_preempt_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] override setting owned_act_preempt_enabled True round-trips."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.owned_act_preempt_enabled is False

    toml = _BASE_TOML.replace(
        "owned_act_preempt_enabled = false",
        "owned_act_preempt_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.owned_act_preempt_enabled is True


def test_config_self_calibration_default_off() -> None:
    """Proposal 129 self-calibration input capture ships disabled (dark launch)."""
    config = Config()
    assert config.audit_self_calibration_enabled is False
    config.validate()


def test_config_self_calibration_committed_toml_default_off() -> None:
    """The committed maestro.toml pins self_calibration_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["audit_self_calibration_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_self_calibration_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """An [audit] overlay setting self_calibration_enabled True round-trips."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.audit_self_calibration_enabled is False

    toml = _BASE_TOML.replace(
        "self_calibration_enabled = false",
        "self_calibration_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.audit_self_calibration_enabled is True


def test_config_refutation_accountability_default_off() -> None:
    """Proposal 129 Phase C refutation accountability ships disabled (dark launch)."""
    config = Config()
    assert config.audit_refutation_accountability_enabled is False
    assert config.audit_refutation_rejection_cycles == 3
    config.validate()


def test_config_refutation_accountability_committed_toml_default_off() -> None:
    """The committed maestro.toml pins refutation_accountability_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["audit_refutation_accountability_enabled"] is False
    assert toml_vals["audit_refutation_rejection_cycles"] == 3


@patch("dotenv.load_dotenv")
def test_config_from_env_refutation_accountability_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """An [audit] overlay setting refutation_accountability_enabled True round-trips."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.audit_refutation_accountability_enabled is False

    toml = _BASE_TOML.replace(
        "refutation_accountability_enabled = false",
        "refutation_accountability_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.audit_refutation_accountability_enabled is True


def test_config_multi_pr_proposals_default_off() -> None:
    """Proposal 118 chain emission ships disabled (safe/experimental)."""
    config = Config()
    assert config.multi_pr_proposals_enabled is False
    config.validate()


def test_config_multi_pr_proposals_committed_toml_default_off() -> None:
    """The committed maestro.toml pins multi_pr_proposals_enabled off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["multi_pr_proposals_enabled"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_multi_pr_proposals_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] override setting multi_pr_proposals_enabled True round-trips."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.multi_pr_proposals_enabled is False

    toml = _BASE_TOML.replace(
        "multi_pr_proposals_enabled = false",
        "multi_pr_proposals_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.multi_pr_proposals_enabled is True


def test_config_unwinnable_race_re_entry_threshold_default() -> None:
    """Proposal 099 (step 8): the base-advance re-entry escalation threshold
    defaults to 3 (the escalation itself is now unconditional — proposal 117
    collapsed the flags)."""
    config = Config()
    assert config.unwinnable_race_re_entry_threshold == 3
    config.validate()


def test_config_unwinnable_race_threshold_committed_toml_default() -> None:
    """The committed maestro.toml pins the re-entry threshold at 3."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["unwinnable_race_re_entry_threshold"] == 3


@patch("dotenv.load_dotenv")
def test_config_from_env_unwinnable_race_threshold_overlay(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] override raising the re-entry threshold round-trips through
    from_toml (the base default is 3)."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.unwinnable_race_re_entry_threshold == 3

    toml = _BASE_TOML.replace(
        "unwinnable_race_re_entry_threshold = 3",
        "unwinnable_race_re_entry_threshold = 5",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.unwinnable_race_re_entry_threshold == 5


def test_config_standdown_refutation_release_cap_default() -> None:
    """Proposal 135: the refuted-release budget defaults to 2 — a handoff-ready
    latch may be taken back twice before it becomes permanent."""
    config = Config()
    assert config.standdown_refutation_release_cap == 2
    config.validate()


def test_config_standdown_refutation_release_cap_committed_toml() -> None:
    """The committed maestro.toml pins the same budget as the dataclass."""
    assert _committed_toml_vals()["standdown_refutation_release_cap"] == 2


@patch("dotenv.load_dotenv")
def test_config_from_env_standdown_refutation_release_cap_overlay(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] override of the release budget round-trips through from_toml."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.standdown_refutation_release_cap == 2

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "standdown_refutation_release_cap = 2",
        "standdown_refutation_release_cap = 5",
    )
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.standdown_refutation_release_cap == 5


def test_active_refutation_release_cap_zero_while_flag_off() -> None:
    """The single-owner derived cap reads 0 while the master flag is off, so
    every ``deliver_handoff`` site skips the ``refutation_releases`` probe and
    the default-off path stays byte-identical."""
    config = Config(
        refutable_standdown_enabled=False,
        standdown_refutation_release_cap=5,
    )
    assert config.active_refutation_release_cap == 0


def test_active_refutation_release_cap_passes_budget_through_when_on() -> None:
    """With the master flag on, the derived cap is the configured budget —
    the release path and the inspection projection read the same number."""
    config = Config(
        refutable_standdown_enabled=True,
        standdown_refutation_release_cap=5,
    )
    assert config.active_refutation_release_cap == 5


@pytest.mark.parametrize("bad", ["0", "true"])
@patch("dotenv.load_dotenv")
def test_config_standdown_refutation_release_cap_rejects_non_positive(
    mock_load: object, mem_fs: dict[str, str], bad: str,
) -> None:
    """``ge=1`` refuses a zero budget — a cap of 0 would make every latch
    terminal on its first refutation — and ``_NumericInt`` refuses ``true``,
    which pydantic would otherwise coerce to a silent cap of 1."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "standdown_refutation_release_cap = 2",
        f"standdown_refutation_release_cap = {bad}",
    )
    with pytest.raises(ValueError, match="standdown_refutation_release_cap"):
        Config.from_env(config_dir=Path("/fake/toml"))


def test_config_require_claude_md_update_default_off() -> None:
    """Proposal 108: the CLAUDE.md-maintenance mandate ships opt-in (default off)."""
    config = Config()
    assert config.require_claude_md_update is False
    config.validate()


def test_config_require_claude_md_update_committed_toml_default_off() -> None:
    """The committed maestro.toml pins require_claude_md_update off."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["require_claude_md_update"] is False


@patch("dotenv.load_dotenv")
def test_config_from_env_require_claude_md_update_overlay_true(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] override setting require_claude_md_update True round-trips."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.require_claude_md_update is False

    toml = _BASE_TOML.replace(
        "require_claude_md_update = false",
        "require_claude_md_update = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.require_claude_md_update is True


def test_config_ci_pending_hold_default_on() -> None:
    """The CI-pending remediation-slot yield ships enabled (default on)."""
    config = Config()
    assert config.ci_pending_hold_dequeue_enabled is True
    config.validate()


def test_config_ci_pending_hold_committed_toml_default_on() -> None:
    """The committed maestro.toml pins ci_pending_hold_dequeue_enabled on."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["ci_pending_hold_dequeue_enabled"] is True


@patch("dotenv.load_dotenv")
def test_config_from_env_ci_pending_hold_overlay_false(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A [loop] override setting ci_pending_hold_dequeue_enabled False round-trips."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.ci_pending_hold_dequeue_enabled is True

    toml = _BASE_TOML.replace(
        "ci_pending_hold_dequeue_enabled = true",
        "ci_pending_hold_dequeue_enabled = false",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.ci_pending_hold_dequeue_enabled is False


def test_config_owned_committed_push_actuator_default_on() -> None:
    """The committed-but-unpushed push actuator ships enabled by default (promoted)."""
    config = Config()
    assert config.owned_committed_push_actuator_enabled is True
    config.validate()


def test_config_owned_committed_push_actuator_committed_toml_default_on() -> None:
    """The committed maestro.toml pins owned_committed_push_actuator_enabled on."""
    toml_vals = _committed_toml_vals()
    assert toml_vals["owned_committed_push_actuator_enabled"] is True


@patch("dotenv.load_dotenv")
def test_config_from_env_owned_committed_push_actuator_overlay_false(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """The committed toml overlays owned_committed_push_actuator_enabled on; a [loop]
    overlay flipping it back off parses through (the flag is promoted default-on)."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.owned_committed_push_actuator_enabled is True

    toml = _BASE_TOML.replace(
        "owned_committed_push_actuator_enabled = true",
        "owned_committed_push_actuator_enabled = false",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.owned_committed_push_actuator_enabled is False


def test_config_progress_aware_lap_budget_default_off() -> None:
    """Proposal 089's progress-aware lap budget ships disabled."""
    config = Config()
    assert config.progress_aware_lap_budget_enabled is False
    config.validate()


def test_config_dropped_directive_replay_default_off() -> None:
    """Proposal 090's dropped-directive replay gate ships disabled and inert."""
    config = Config()
    assert config.dropped_directive_replay_enabled is False
    config.validate()


def test_config_investigation_observable_gate_default_on() -> None:
    """The investigation-observable gate ships enabled so it is falsifiable."""
    config = Config()
    assert config.investigation_observable_gate_enabled is True
    config.validate()


def test_config_plan_grounding_default_off() -> None:
    """Plan-premise grounding ships disabled — flag-off is byte-identical (114)."""
    config = Config()
    assert config.plan_grounding_enabled is False
    config.validate()


def test_config_generator_reality_grounding_default_off() -> None:
    """Generator reality grounding ships disabled — flag-off is byte-identical (136)."""
    config = Config()
    assert config.generator_reality_grounding_enabled is False
    config.validate()


def test_config_tend_planless_prs_default_on() -> None:
    """Proposal 091's planless-PR tending ships enabled by default."""
    config = Config()
    assert config.tend_planless_prs_enabled is True
    config.validate()


def test_config_resolve_addressed_threads_default_on() -> None:
    """Proposal 091's addressed-thread auto-resolution ships enabled by default."""
    config = Config()
    assert config.resolve_addressed_threads_enabled is True
    config.validate()


def test_config_gate_strip_redirections_default_on() -> None:
    """Proposal 096's redirection-stripping gate flag ships enabled by default."""
    config = Config()
    assert config.gate_strip_redirections_enabled is True
    config.validate()


def test_config_user_request_blocks_standdown_default_on() -> None:
    """The human-demand stand-down veto (proposal 091) ships enabled by default."""
    config = Config()
    assert config.user_request_blocks_standdown_enabled is True
    config.validate()


def test_config_user_pr_message_remediation_default_on() -> None:
    """Proposal 091's human-PR-message routing ships enabled by default."""
    config = Config()
    assert config.user_pr_message_remediation_enabled is True
    config.validate()


@pytest.mark.parametrize("bad", [0, -1, True, 2.0, "3", None])
def test_config_rejects_invalid_probe_metrics_every(bad: object) -> None:
    """A non-positive (or non-int) probe cadence fails loud at config load with
    a message naming the key — never a silent probe-every-turn."""
    config = Config(sdk_cli_paced_probe_metrics_every=bad)  # type: ignore[arg-type]
    with pytest.raises(
        ValueError,
        match="sdk_cli_paced_probe_metrics_every must be an integer >= 1",
    ):
        config.validate()


# ---------------------------------------------------------------------
# cli_paced marker pack (proposal 075 Step 4)
# ---------------------------------------------------------------------


def _register_test_pack(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Install a one-pack registry so a *named* setting can resolve.

    Patches both the defining module's global (what ``get_marker_pack``'s
    default-argument lookup reads) and the package re-export (what
    ``Config.validate``'s error message enumerates).
    """
    from maestro_sdk import MarkerPack
    from maestro_sdk import marker_packs as marker_packs_module

    pack = MarkerPack(name=name, min_version="2.0.0", max_version="2.9.9")
    monkeypatch.setattr(marker_packs_module, "MARKER_PACKS", (pack,))
    import maestro_sdk

    monkeypatch.setattr(maestro_sdk, "MARKER_PACKS", (pack,))


def test_config_marker_pack_defaults_to_none_mode() -> None:
    config = Config()
    assert config.sdk_cli_paced_marker_pack == "none"
    config.validate()


def test_config_marker_pack_accepts_auto_and_none() -> None:
    Config(sdk_cli_paced_marker_pack="auto").validate()
    Config(sdk_cli_paced_marker_pack="none").validate()


@pytest.mark.parametrize("bad", ["", "   ", None, 3, True])
def test_config_marker_pack_rejects_non_string_or_blank(bad: object) -> None:
    config = Config(sdk_cli_paced_marker_pack=bad)  # type: ignore[arg-type]
    with pytest.raises(
        ValueError, match="sdk_cli_paced_marker_pack must be"
    ):
        config.validate()


def test_config_marker_pack_unknown_name_fails_loud_when_cli_paced_selected() -> None:
    """A typo'd pack name on a cli_paced deployment must not silently mean
    "no markers" — it fails at config load, naming the registered packs."""
    config = Config(
        sdk_provider="cli_paced", sdk_cli_paced_marker_pack="no-such-pack"
    )
    with pytest.raises(
        ValueError, match="names unknown pack 'no-such-pack'"
    ):
        config.validate()


def test_config_marker_pack_unknown_name_fails_loud_for_phase_route() -> None:
    """A per-phase cli_paced route gets the same registry check as a global
    selection — the pack matters wherever cli_paced serves a phase."""
    config = Config(
        sdk_phase_providers={"contemplation": "cli_paced"},
        sdk_cli_paced_marker_pack="no-such-pack",
    )
    with pytest.raises(ValueError, match="names unknown pack"):
        config.validate()


def test_config_marker_pack_known_name_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_test_pack(monkeypatch, "claude-test")
    Config(
        sdk_provider="cli_paced", sdk_cli_paced_marker_pack="claude-test"
    ).validate()


def test_config_marker_pack_name_unchecked_without_cli_paced() -> None:
    """Under the default anthropic provider the named-pack registry check is
    skipped (the key is inert there), keeping the default path free of the
    maestro_sdk import — the documented lazy posture."""
    Config(sdk_cli_paced_marker_pack="no-such-pack").validate()


def test_loop_evolve_every_and_loop_task_every_zero_are_valid() -> None:
    """0 is valid for loop_evolve_every and loop_task_every (means disabled)."""
    Config(loop_evolve_every=0, loop_task_every=0).validate()


def test_loop_curate_every_zero_is_valid() -> None:
    """0 is valid for loop_curate_every (the default; means disabled)."""
    Config(loop_curate_every=0).validate()


def test_loop_curate_defaults_disabled() -> None:
    """The curate phase ships off: every == 0 and enabled is False."""
    config = Config()
    assert config.loop_curate_every == 0
    assert config.loop_curate_enabled is False


def test_loop_curate_min_plan_age_hours_zero_is_valid() -> None:
    """0.0 is valid (the default) — it disables the grace period."""
    Config(loop_curate_min_plan_age_hours=0.0).validate()


def test_loop_curate_min_plan_age_hours_positive_is_valid() -> None:
    """A positive float grace period passes validation."""
    Config(loop_curate_min_plan_age_hours=24.0).validate()


def test_loop_curate_min_plan_age_hours_default_is_zero() -> None:
    """The grace period ships at 0.0 (no delay)."""
    assert Config().loop_curate_min_plan_age_hours == 0.0


def test_loop_curate_skip_serviced_enabled_default_is_false() -> None:
    """The serviced-plan curation skip ships off (criterion 6)."""
    assert Config().loop_curate_skip_serviced_enabled is False


def test_validate_accepts_transcript_content_max_boundary_one() -> None:
    """The boundary value ``1`` is accepted."""
    cfg = Config(transcript_content_max=1)
    cfg.validate()
    assert cfg.transcript_content_max == 1


def test_validate_accepts_cov_fail_under_boundary() -> None:
    Config(cov_fail_under=0).validate()
    Config(cov_fail_under=100).validate()


def test_mutation_rate_limit_defaults() -> None:
    cfg = Config()
    assert cfg.login_rate_limit_per_minute == 10
    assert cfg.approval_rate_limit_per_minute == 30
    cfg.validate()


def test_validate_accepts_zero_executor_empty_response_retries() -> None:
    """``0`` disables the retry and is explicitly allowed."""
    cfg = Config(executor_empty_response_retries=0)
    cfg.validate()


def test_investigation_streak_threshold_default() -> None:
    """Default investigation_streak_threshold is 3."""
    cfg = Config()
    assert cfg.investigation_streak_threshold == 3


def test_investigation_streak_enabled_default() -> None:
    """Default investigation_streak_enabled is True."""
    cfg = Config()
    assert cfg.investigation_streak_enabled is True


def test_branchless_streak_threshold_default() -> None:
    """Default branchless_streak_threshold is 3."""
    cfg = Config()
    assert cfg.branchless_streak_threshold == 3


def test_investigation_rate_window_default() -> None:
    """Default investigation_rate_window is 10."""
    cfg = Config()
    assert cfg.investigation_rate_window == 10


def test_investigation_rate_threshold_default() -> None:
    """Default investigation_rate_threshold is 0.4."""
    cfg = Config()
    assert cfg.investigation_rate_threshold == 0.4


def test_investigation_rate_enabled_default() -> None:
    """Default investigation_rate_enabled is True."""
    cfg = Config()
    assert cfg.investigation_rate_enabled is True


def test_readmission_construction_directive_defaults() -> None:
    """Defaults are flag-off and a 0.5 ratio threshold."""
    cfg = Config()
    assert cfg.readmission_construction_directive_enabled is False
    assert cfg.readmission_construction_ratio_threshold == 0.5


def test_promotable_backlog_directive_enabled_default() -> None:
    """Default promotable_backlog_directive_enabled is True."""
    cfg = Config()
    assert cfg.promotable_backlog_directive_enabled is True


# =====================================================================
# MCP configuration
# =====================================================================


def test_mcp_defaults() -> None:
    config = Config()
    assert not hasattr(config, "fetch_mcp_enabled")
    assert config.context7_mcp_enabled is False
    assert config.context7_mcp_command == "npx"
    assert config.context7_mcp_args == ["-y", "@upstash/context7-mcp"]
    assert config.serena_mcp_enabled is False
    assert config.serena_mcp_command == "uvx"
    assert config.serena_mcp_args == [
        "--from", "serena-agent==1.5.3", "serena", "start-mcp-server",
    ]


def test_serena_mcp_args_pin_an_exact_version() -> None:
    """The committed serena args must pin an exact package version so the

    server's tool inventory cannot drift under us (proposal 084 Step 2).
    """
    config = Config()
    assert "serena-agent==1.5.3" in config.serena_mcp_args


def test_mcp_servers_config_disabled() -> None:
    assert Config().mcp_servers_config() is None


def test_github_mcp_integration_retired() -> None:
    """#4132: the ``[mcp] github_*`` integration is retired — its npm launcher
    404'd and a working server would expose repo-writing tools that bypass the
    ``Bash``-scoped action gate. GitHub access goes through the code host."""
    names = {f.name for f in dataclasses.fields(Config)}
    assert "github_mcp_enabled" not in names
    assert "github_mcp_command" not in names
    assert "github_mcp_args" not in names


@patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test123"})
def test_mcp_servers_config_never_emits_github() -> None:
    config = Config(
        context7_mcp_enabled=True,
        serena_mcp_enabled=True,
    )
    result = config.mcp_servers_config()
    assert result is not None
    assert set(result) == {"context7", "serena"}


def test_mcp_servers_config_custom_command() -> None:
    config = Config(
        context7_mcp_enabled=True,
        context7_mcp_command="node",
        context7_mcp_args=["server.js"],
    )
    result = config.mcp_servers_config()
    assert result is not None
    assert result["context7"]["command"] == "node"
    assert result["context7"]["args"] == ["server.js"]


def test_fetch_mcp_integration_retired() -> None:
    """Proposal 146 Step 11: the repo-mutating Fetch MCP server is retired —
    no MCP server reaches the web.
    No dataclass field, base ``[mcp]`` key, or runtime-contract entry survives."""
    names = {f.name for f in dataclasses.fields(Config)}
    assert not {n for n in names if n.startswith("fetch_mcp")}
    mcp = tomllib.loads(_BASE_TOML)["mcp"]
    assert not [k for k in mcp if k.startswith("fetch_")]
    for entry in load_contract().entries:
        assert entry.name != "mcp-server-fetch"
        assert "config:fetch_mcp_enabled" not in entry.required_when
    result = Config(context7_mcp_enabled=True, serena_mcp_enabled=True).mcp_servers_config()
    assert result is not None
    assert "fetch" not in result


def test_mcp_servers_config_context7_enabled() -> None:
    config = Config(context7_mcp_enabled=True)
    result = config.mcp_servers_config()
    assert result is not None
    assert "context7" in result
    assert result["context7"]["command"] == "npx"
    assert result["context7"]["args"] == ["-y", "@upstash/context7-mcp"]
    assert "env" not in result["context7"]


def test_mcp_servers_config_serena_enabled() -> None:
    config = Config(serena_mcp_enabled=True)
    result = config.mcp_servers_config()
    assert result is not None
    assert "serena" in result
    assert result["serena"]["command"] == "uvx"
    assert result["serena"]["args"] == [
        "--from", "serena-agent==1.5.3", "serena", "start-mcp-server",
    ]
    assert "env" not in result["serena"]


def test_mcp_servers_config_serena_disabled_byte_identical() -> None:
    """With serena off and nothing else enabled, the dict is exactly today's."""
    assert Config(serena_mcp_enabled=False).mcp_servers_config() is None


def test_mcp_servers_config_multiple_enabled() -> None:
    config = Config(
        context7_mcp_enabled=True,
        serena_mcp_enabled=True,
    )
    result = config.mcp_servers_config()
    assert result is not None
    assert "fetch" not in result
    assert "context7" in result
    assert "serena" in result


def test_check_mcp_launchers_no_external_server() -> None:
    """No external server enabled → no-op, and PATH is never probed."""
    config = Config()
    with patch("maestro.config.shutil.which", return_value=None) as which:
        assert config.check_mcp_launchers() == []
    which.assert_not_called()


def test_check_mcp_launchers_all_resolve() -> None:
    """Every enabled launcher on PATH → no errors."""
    config = Config(serena_mcp_enabled=True, context7_mcp_enabled=True)
    with patch("maestro.config.shutil.which", return_value="/usr/bin/launcher"):
        assert config.check_mcp_launchers() == []


def test_check_mcp_launchers_missing_launcher() -> None:
    """An enabled server whose launcher is absent yields one actionable message."""
    config = Config(serena_mcp_enabled=True)

    def fake_which(cmd: str) -> str | None:
        return None if cmd == "uvx" else f"/usr/bin/{cmd}"

    with patch("maestro.config.shutil.which", side_effect=fake_which):
        errors = config.check_mcp_launchers()
    assert len(errors) == 1
    assert "serena" in errors[0]
    assert "uvx" in errors[0]
    assert "serena_enabled = false" in errors[0]


def test_check_mcp_launchers_one_message_per_missing_server() -> None:
    """One message per enabled server whose launcher is missing."""
    config = Config(serena_mcp_enabled=True, context7_mcp_enabled=True)
    with patch("maestro.config.shutil.which", return_value=None):
        errors = config.check_mcp_launchers()
    assert len(errors) == 2
    joined = "\n".join(errors)
    assert "serena" in joined
    assert "context7" in joined


def test_check_mcp_launchers_hint_names_toolchain() -> None:
    """The remediation hint names the toolchain that ships each launcher —
    `uv` for `uvx`, Node.js for `npx` — not the launcher binary itself."""
    config = Config(serena_mcp_enabled=True, context7_mcp_enabled=True)
    with patch("maestro.config.shutil.which", return_value=None):
        errors = config.check_mcp_launchers()
    by_server = {e.split(" ", 1)[0]: e for e in errors}
    # serena → uvx → ships with uv
    assert "install uv" in by_server["serena"]
    # context7 → npx → ships with Node.js
    assert "Node.js" in by_server["context7"]


def test_check_mcp_launchers_unknown_launcher_generic_hint() -> None:
    """A launcher with no toolchain mapping falls back to ``install <command>``."""
    config = Config(serena_mcp_enabled=True, serena_mcp_command="weirdtool")
    with patch("maestro.config.shutil.which", return_value=None):
        errors = config.check_mcp_launchers()
    assert len(errors) == 1
    assert "install weirdtool" in errors[0]


def test_check_mcp_launchers_honors_passed_servers() -> None:
    """When a caller passes a pre-built spec dict, it is used verbatim and
    ``mcp_servers_config`` is not rebuilt (avoids a second env read)."""
    config = Config()  # no external server enabled
    servers = {"github": {"command": "npx", "args": []}}
    with (
        patch.object(
            config, "mcp_servers_config", side_effect=AssertionError("rebuilt")
        ),
        patch("maestro.config.shutil.which", return_value=None),
    ):
        errors = config.check_mcp_launchers(servers)
    assert len(errors) == 1
    assert "github" in errors[0]


# =====================================================================
# Config.external_mcp_disallowed_tools
# =====================================================================


def test_external_mcp_disallowed_tools_off_returns_none() -> None:
    """Off → None so QueryOptions keeps the default ``["Agent"]`` denial."""
    assert Config(serena_mcp_enabled=False).external_mcp_disallowed_tools() is None


def test_external_mcp_disallowed_tools_on_denies_exec_and_writes() -> None:
    """On → ``Agent`` plus every serena mutating / shell-exec tool by name."""
    from maestro.config import SERENA_MUTATING_TOOLS

    result = Config(serena_mcp_enabled=True).external_mcp_disallowed_tools()
    assert result is not None
    assert "Agent" in result
    # The shell-exec carrier that could bypass the Bash action gate is denied.
    assert "mcp__serena__execute_shell_command" in result
    for tool in SERENA_MUTATING_TOOLS:
        assert f"mcp__serena__{tool}" in result


def test_external_mcp_disallowed_tools_denies_pinned_1_5_3_mutating_surface() -> None:
    """The pinned serena-agent==1.5.3 mutating surface is denied by name.

    These write/exec tools were absent from ``SERENA_MUTATING_TOOLS`` while the
    tuple drifted from the pinned inventory (#3108): five symbol/memory editors
    caught in the first pass, plus the three file line-editors
    (``delete_lines`` / ``replace_lines`` / ``insert_at_line``, defined in
    1.5.3's ``file_tools.py``) the follow-up review found still missing. Because
    the guard is a deny-list, an omitted mutating tool passed straight through
    and could write repo files outside the ``Bash``-scoped action gate. Pin the
    concrete names as literals — independent of the tuple's contents — so a
    future inventory drift that drops one fails loudly here.
    """
    result = Config(serena_mcp_enabled=True).external_mcp_disallowed_tools()
    assert result is not None
    for tool in (
        "mcp__serena__rename_symbol",
        "mcp__serena__replace_content",
        "mcp__serena__safe_delete_symbol",
        "mcp__serena__edit_memory",
        "mcp__serena__rename_memory",
        "mcp__serena__delete_lines",
        "mcp__serena__replace_lines",
        "mcp__serena__insert_at_line",
    ):
        assert tool in result
    # The shell-exec carrier stays denied alongside the newly-covered names.
    assert "mcp__serena__execute_shell_command" in result


def test_external_mcp_disallowed_tools_keeps_read_tools_reachable() -> None:
    """Read tools are never in the denial set, so retrieval stays reachable."""
    result = Config(serena_mcp_enabled=True).external_mcp_disallowed_tools()
    assert result is not None
    for read_tool in ("find_symbol", "get_symbols_overview", "read_file"):
        assert f"mcp__serena__{read_tool}" not in result


# =====================================================================
# Config.from_env
# =====================================================================


@patch("dotenv.load_dotenv")
def test_config_from_env(mock_load: object, mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.verbose is False
    assert config.dashboard_port == 8420


@patch("dotenv.load_dotenv")
def test_config_from_env_verbose_from_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """verbose is loaded from TOML, not env var."""
    toml = _BASE_TOML.replace("verbose = false", "verbose = true")
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.verbose is True


@patch("dotenv.load_dotenv")
def test_config_from_env_sdk_model_from_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """[models].default is loaded into Config.sdk_model."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.sdk_model == model_profiles.DEFAULT_MODEL


@patch.dict("os.environ", {"MAESTRO_ENV": "test"})
@patch("dotenv.load_dotenv")
def test_config_from_env_sdk_model_overlay_overrides(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """An environment overlay can override [models].default."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    mem_fs["/fake/toml/maestro.test.toml"] = (
        '[models]\ndefault = "claude-sonnet-4-6"\n'
    )
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.sdk_model == "claude-sonnet-4-6"


@patch("dotenv.load_dotenv")
def test_config_from_env_github_repo_from_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """github_repo loaded from TOML; empty string becomes None."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.github_repo is None

    toml = _BASE_TOML.replace('repo = ""', 'repo = "owner/repo"')
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.github_repo == "owner/repo"


@patch("dotenv.load_dotenv")
def test_config_from_env_github_reviewer_from_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """github_reviewer loaded from TOML; empty string becomes None."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.github_reviewer is None

    toml = _BASE_TOML.replace('reviewer = ""', 'reviewer = "thomas-sodon"')
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.github_reviewer == "thomas-sodon"


@patch("dotenv.load_dotenv")
def test_config_from_env_max_consecutive_remediation_from_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """[loop].max_consecutive_remediation loads its default and honors an
    override from maestro.toml."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    # Default matches the committed maestro.toml value.
    assert config.max_consecutive_remediation == 4

    toml = _BASE_TOML.replace(
        "max_consecutive_remediation = 4",
        "max_consecutive_remediation = 2",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.max_consecutive_remediation == 2


def test_config_accepts_zero_max_consecutive_remediation() -> None:
    """Zero disables the consecutive-remediation cap — a valid configuration
    that preserves remediation's unconditional precedence."""
    Config(max_consecutive_remediation=0).validate()


@patch("dotenv.load_dotenv")
def test_config_from_env_email(
    mock_load: object, monkeypatch: object, mem_fs: dict[str, str],
) -> None:
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    monkeypatch.setenv("MAESTRO_EMAIL_ADDRESS", "agent@test.xyz")  # type: ignore[attr-defined]
    monkeypatch.setenv("MAESTRO_EMAIL_PASSWORD", "secret123")  # type: ignore[attr-defined]
    monkeypatch.setenv("MAESTRO_EMAIL_THOMAS", "thomas@test.xyz")  # type: ignore[attr-defined]
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.email_address == "agent@test.xyz"
    assert config.email_password == "secret123"
    assert config.email_thomas == "thomas@test.xyz"
    assert config.email_smtp_host == "smtp.zoho.com"
    assert config.email_smtp_port == 587


@patch("dotenv.load_dotenv")
def test_config_from_env_telegram_chat_id_from_env_var(
    mock_load: object, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """telegram_chat_id loaded from TELEGRAM_CHAT_ID env var."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.telegram_chat_id is None

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.telegram_chat_id == "123456789"


@patch("dotenv.load_dotenv")
def test_config_from_env_workspace_tilde_expansion(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """Workspace path supports ~ expansion."""
    toml = _BASE_TOML.replace('workspace = "."', 'workspace = "~/projects/maestro"')
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert "~" not in str(config.workspace)
    assert str(config.workspace).endswith("projects/maestro")


@patch("dotenv.load_dotenv")
def test_config_from_env_memory_dir_tilde_expansion(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """Memory dir path supports ~ expansion."""
    toml = _BASE_TOML.replace('memory_dir = "data"', 'memory_dir = "~/.maestro"')
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert "~" not in str(config.memory_dir)


@patch("dotenv.load_dotenv")
def test_config_from_env_workspace_relative_path(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """Relative workspace path is resolved against cwd."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(config_dir=Path("/fake/toml"))
    # "." resolves to cwd, which should be absolute
    assert config.workspace.is_absolute()


@patch("dotenv.load_dotenv")
def test_config_from_env_memory_dir_relative_path(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """Relative memory_dir path is resolved against cwd."""
    toml = _BASE_TOML.replace('memory_dir = "data"', 'memory_dir = "state"')
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.memory_dir.is_absolute()
    assert config.memory_dir.name == "state"


@patch("dotenv.load_dotenv")
def test_config_from_env_loads_dotenv(mock_load: object, mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    Config.from_env(config_dir=Path("/fake/toml"))
    mock_load.assert_called_once_with(".env", override=False)  # type: ignore[attr-defined]


@patch("dotenv.load_dotenv")
def test_config_from_env_custom_env_file(mock_load: object, mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    Config.from_env(env_file="/custom/.env", config_dir=Path("/fake/toml"))
    mock_load.assert_called_once_with("/custom/.env", override=False)  # type: ignore[attr-defined]


@patch("dotenv.load_dotenv")
def test_config_from_env_skip_dotenv(mock_load: object, mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    mock_load.assert_not_called()  # type: ignore[attr-defined]


@patch("dotenv.load_dotenv")
def test_config_from_env_calls_validate(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """from_env() should call validate() to catch bad TOML values."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    Config.from_env(config_dir=Path("/fake/toml"))


@patch("dotenv.load_dotenv")
def test_config_from_env_rejects_negative_timeout_in_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """A negative timeout is rejected: the section model's ``gt=0`` constraint
    raises, and ``from_env`` normalises it to a plain ``ValueError``."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace("git = 30", "git = -5")
    with pytest.raises(ValueError, match="git"):
        Config.from_env(config_dir=Path("/fake/toml"))


@patch("dotenv.load_dotenv")
def test_config_from_env_dashboard_host_from_toml(
    mock_load: object,
    mem_fs: dict[str, str],
) -> None:
    """Dashboard host is loaded from TOML only (no env var override)."""
    toml = _BASE_TOML.replace('host = "0.0.0.0"', 'host = "127.0.0.1"')
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.dashboard_host == "127.0.0.1"


# =====================================================================
# Config TOML integration
# =====================================================================


@patch("dotenv.load_dotenv")
def test_config_from_env_with_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """TOML values flow into Config."""
    toml = _BASE_TOML.replace("git = 30", "git = 120").replace("port = 8420", "port = 9999")
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.git_timeout == 120
    assert config.dashboard_port == 9999


@patch("dotenv.load_dotenv")
def test_config_dashboard_port_from_toml(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """Dashboard port is loaded from TOML only (no env var override)."""
    toml = _BASE_TOML.replace("port = 8420", "port = 9999")
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.dashboard_port == 9999


@patch("dotenv.load_dotenv")
def test_config_from_env_with_maestro_env(
    mock_load: object, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAESTRO_ENV selects the overlay file."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    mem_fs["/fake/toml/maestro.staging.toml"] = "[timeouts]\ngit = 90\n"
    monkeypatch.setenv("MAESTRO_ENV", "staging")
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.git_timeout == 90


# =====================================================================
# check_auth / auth_label
# =====================================================================


def test_check_auth_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert Config.check_auth() == "oauth"


def test_check_auth_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert Config.check_auth() == "api_key"


def test_check_auth_prefers_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert Config.check_auth() == "oauth"


def test_check_auth_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="No Claude authentication found"):
        Config.check_auth()


def test_auth_label_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert Config.auth_label() == "OAuth token (Max/Pro subscription)"


def test_auth_label_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    assert Config.auth_label() == "API key"


def test_auth_label_reuses_provided_method() -> None:
    """auth_label(method) reuses a known method without re-reading auth env (#356).

    When the caller passes the already-resolved ``check_auth`` verdict,
    auth_label must map it to the label directly and never call
    ``check_auth`` a second time.
    """
    with patch.object(Config, "check_auth") as spy:
        assert Config.auth_label("oauth") == "OAuth token (Max/Pro subscription)"
        assert Config.auth_label("api_key") == "API key"
    spy.assert_not_called()


def test_auth_label_unknown_method_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected auth_method value falls through to check_auth (#356).

    The narrow known-value allowlist means a malformed passthrough re-resolves
    from the env rather than silently mislabelling.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    with patch.object(Config, "check_auth", return_value="api_key") as spy:
        assert Config.auth_label("bogus") == "API key"
    spy.assert_called_once()


def test_auth_label_non_anthropic_provider_is_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth_label never resolves anthropic creds for a non-anthropic provider (#356 review).

    A correctly-configured non-anthropic deployment (its own key present,
    anthropic creds absent) must get a provider-neutral label from the label
    step rather than crashing on the anthropic ``check_auth()`` fallback — the
    active provider's own credential was already validated by ``check_secrets``.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with patch.object(Config, "check_auth") as spy:
        label = Config.auth_label(None, provider="openai")
    assert label == "openai provider credential"
    spy.assert_not_called()


# =====================================================================
# Module-level import checks
# =====================================================================


def test_math_imported_at_module_level() -> None:
    """math must be imported at module level, not inside validate()."""
    import maestro.config as config_module

    source = inspect.getsource(config_module)
    tree = ast.parse(source)

    module_level_names = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_level_names.add(alias.asname or alias.name)

    assert "math" in module_level_names, (
        "'math' must be imported at module level in config.py"
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "validate":
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        assert alias.name != "math", (
                            "'import math' found inside validate()"
                        )


def test_import_copy_is_at_module_top_level() -> None:
    """import copy must appear at the module top level in toml_config.py."""
    source = (Path(__file__).resolve().parent.parent / "maestro" / "toml_config.py").read_text()
    tree = ast.parse(source)

    top_level_names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            top_level_names.append(node.module)

    assert "copy" in top_level_names, (
        "'import copy' must be at the module top level in toml_config.py"
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        assert alias.name != "copy", (
                            f"'import copy' found inside function '{node.name}'"
                        )


# =====================================================================
# check_secrets
# =====================================================================


def test_check_secrets_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """No warnings when all secrets are present and well-formed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "my-secret-token")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    warnings = Config.check_secrets()
    assert warnings == []


def test_check_secrets_reuses_provided_auth_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_secrets(auth_method=...) skips the internal check_auth call (#356).

    When the CLI has already resolved the auth method it threads it through so
    the anthropic provider-credential precondition reuses it instead of
    re-reading the auth env vars — validating the active credential's prefix
    off the reused method.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    with patch.object(Config, "check_auth") as spy:
        warnings = Config.check_secrets(provider="anthropic", auth_method="api_key")
    spy.assert_not_called()
    assert not any("ANTHROPIC_API_KEY has unexpected prefix" in w for w in warnings)


def test_check_secrets_unknown_auth_method_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected auth_method still resolves via check_auth (#356).

    The narrow known-value allowlist keeps a malformed passthrough failing
    loud (re-resolving) rather than silently accepted.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    with patch.object(Config, "check_auth", return_value="api_key") as spy:
        Config.check_secrets(provider="anthropic", auth_method="bogus")
    spy.assert_called_once()


def test_check_secrets_missing_github_token_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit when GITHUB_TOKEN is missing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GITHUB_TOKEN is not set"):
        Config.check_secrets()


def test_check_secrets_missing_claude_auth_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemExit when no Claude credential is set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    with pytest.raises(SystemExit, match="No Claude authentication found"):
        Config.check_secrets()


def test_check_secrets_bad_api_key_prefix_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning when ANTHROPIC_API_KEY has unexpected prefix."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-prefix-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets()
    assert any("ANTHROPIC_API_KEY has unexpected prefix" in w for w in warnings)


def test_check_secrets_bad_oauth_prefix_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning when CLAUDE_CODE_OAUTH_TOKEN has unexpected prefix."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "bad-oauth-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets()
    assert any("CLAUDE_CODE_OAUTH_TOKEN has unexpected prefix" in w for w in warnings)


def test_check_secrets_bad_github_token_prefix_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning when GITHUB_TOKEN has unexpected prefix."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "bad_prefix_token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets()
    assert any("GITHUB_TOKEN has unexpected prefix" in w for w in warnings)


def test_check_secrets_ghu_github_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """ghu_-prefixed GitHub tokens (user-to-server OAuth) are accepted without warning."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghu_testOAuthUserToken1234567890abcdef")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets()
    assert not any("GITHUB_TOKEN has unexpected prefix" in w for w in warnings)


def test_check_secrets_ghs_github_token_passes_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ghs_-prefixed GitHub tokens (GitHub Actions) are accepted without warning.

    Baseline case: ``ghs_`` has been an accepted prefix since the original
    secrets-health-check commit, predating the ``ghu_`` addition that
    ``test_check_secrets_ghu_github_token_passes`` guards. This test documents
    the pre-existing acceptance path, not one introduced alongside ``ghu_``.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_testActionsToken1234567890abcdef")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets()
    assert not any("GITHUB_TOKEN has unexpected prefix" in w for w in warnings)


def test_check_secrets_missing_telegram_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning when TELEGRAM_BOT_TOKEN is missing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets()
    assert any("TELEGRAM_BOT_TOKEN not set" in w for w in warnings)
    assert any("TELEGRAM_CHAT_ID not set" in w for w in warnings)


def test_check_secrets_bad_telegram_format_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning when TELEGRAM_BOT_TOKEN has bad format."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-valid-token")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets()
    assert any("TELEGRAM_BOT_TOKEN has unexpected format" in w for w in warnings)


def test_check_secrets_missing_dashboard_token_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning when MAESTRO_DASHBOARD_TOKEN is missing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.delenv("MAESTRO_DASHBOARD_TOKEN", raising=False)
    warnings = Config.check_secrets()
    assert any("MAESTRO_DASHBOARD_TOKEN not set" in w for w in warnings)


# =====================================================================
# check_secrets — proposal 058 provider-aware credential dispatch
# =====================================================================


def test_check_secrets_openai_provider_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='openai' validates OPENAI_API_KEY, not the Anthropic creds."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test000")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets(provider="openai")
    assert warnings == []


def test_check_secrets_openai_missing_key_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='openai' raises with its own remediation when key absent."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    with pytest.raises(SystemExit, match="OPENAI_API_KEY is not set"):
        Config.check_secrets(provider="openai")


def test_check_secrets_openai_bad_prefix_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='openai' warns when OPENAI_API_KEY has an unexpected prefix."""
    monkeypatch.setenv("OPENAI_API_KEY", "bad-prefix-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    warnings = Config.check_secrets(provider="openai")
    assert any("OPENAI_API_KEY has unexpected prefix" in w for w in warnings)


def test_check_secrets_cli_paced_passes_when_on_path_and_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='cli_paced' passes when `claude` is on PATH and logged in."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")
    monkeypatch.setattr(config_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        config_module.Config, "_claude_cli_logged_in", staticmethod(lambda: True)
    )
    warnings = Config.check_secrets(provider="cli_paced")
    assert warnings == []


def test_check_secrets_cli_paced_missing_claude_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='cli_paced' raises with its remediation when `claude` absent."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setattr(config_module.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit, match="claude auth login"):
        Config.check_secrets(provider="cli_paced")


def test_check_secrets_cli_paced_unauthenticated_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude` on PATH but not logged in raises the `claude auth login` hint."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setattr(config_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        config_module.Config, "_claude_cli_logged_in", staticmethod(lambda: False)
    )
    with pytest.raises(SystemExit, match="not logged in"):
        Config.check_secrets(provider="cli_paced")


# =====================================================================
# check_secrets — proposal 067 phase-aware per-phase provider credentials
# =====================================================================


def _full_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every required + optional secret for the default anthropic path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")


def test_mutating_phase_names_subset_of_sdk_phase_names() -> None:
    """The must-execute phase set stays within the per-phase routing allowlist."""
    assert config_module.MUTATING_SDK_PHASE_NAMES <= config_module.SDK_PHASE_NAMES


def test_mutating_phase_names_match_action_gated_phases() -> None:
    """Drift guard: the must-execute set mirrors 066's action-gated set today."""
    from maestro.tool_surface import action_gated_phases

    assert action_gated_phases() == config_module.MUTATING_SDK_PHASE_NAMES


def test_phase_cli_paced_mutating_logged_out_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cli_paced routed per-phase to run_task with no CLI login fails hard."""
    _full_anthropic_env(monkeypatch)
    monkeypatch.setattr(config_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        config_module.Config, "_claude_cli_logged_in", staticmethod(lambda: False)
    )
    with pytest.raises(SystemExit, match="run_task") as excinfo:
        Config.check_secrets(
            provider="anthropic", phase_providers={"run_task": "cli_paced"}
        )
    assert "claude auth login" in str(excinfo.value)


def test_phase_cli_paced_implementation_missing_cli_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cli_paced routed to implementation with no `claude` on PATH fails hard."""
    _full_anthropic_env(monkeypatch)
    monkeypatch.setattr(config_module.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit, match="implementation") as excinfo:
        Config.check_secrets(
            provider="anthropic", phase_providers={"implementation": "cli_paced"}
        )
    assert "claude auth login" in str(excinfo.value)


def test_phase_cli_paced_nonmutating_logged_out_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cli_paced routing only a non-mutating phase degrades to a warning."""
    _full_anthropic_env(monkeypatch)
    monkeypatch.setattr(config_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        config_module.Config, "_claude_cli_logged_in", staticmethod(lambda: False)
    )
    warnings = Config.check_secrets(
        provider="anthropic", phase_providers={"contemplation": "cli_paced"}
    )
    matching = [w for w in warnings if "contemplation" in w]
    assert len(matching) == 1
    assert "claude auth login" in matching[0]


def test_phase_cli_paced_logged_in_passes_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logged-in CLI routed to a mutating phase produces no warning."""
    _full_anthropic_env(monkeypatch)
    monkeypatch.setattr(config_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        config_module.Config, "_claude_cli_logged_in", staticmethod(lambda: True)
    )
    warnings = Config.check_secrets(
        provider="anthropic", phase_providers={"run_task": "cli_paced"}
    )
    assert warnings == []


def test_phase_cli_paced_nonmutating_logged_in_passes_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logged-in CLI routed to a non-mutating phase produces no warning."""
    _full_anthropic_env(monkeypatch)
    monkeypatch.setattr(config_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        config_module.Config, "_claude_cli_logged_in", staticmethod(lambda: True)
    )
    warnings = Config.check_secrets(
        provider="anthropic", phase_providers={"contemplation": "cli_paced"}
    )
    assert warnings == []


def test_phase_cli_paced_mixed_phases_logged_out_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider routed to mutating AND non-mutating phases gets the hard verdict."""
    _full_anthropic_env(monkeypatch)
    monkeypatch.setattr(config_module.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        config_module.Config, "_claude_cli_logged_in", staticmethod(lambda: False)
    )
    with pytest.raises(SystemExit, match="run_task"):
        Config.check_secrets(
            provider="anthropic",
            phase_providers={
                "contemplation": "cli_paced",
                "run_task": "cli_paced",
            },
        )


def test_phase_provider_same_as_global_checked_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-phase route to the global provider is not re-checked."""
    _full_anthropic_env(monkeypatch)
    calls: list[str] = []
    original = Config._check_provider_credential

    def _spy(provider: str, auth_method: str | None = None) -> list[str]:
        calls.append(provider)
        return original(provider, auth_method=auth_method)

    monkeypatch.setattr(
        config_module.Config, "_check_provider_credential", staticmethod(_spy)
    )
    warnings = Config.check_secrets(
        provider="anthropic", phase_providers={"contemplation": "anthropic"}
    )
    assert warnings == []
    assert calls == ["anthropic"]


def test_phase_openai_mutating_missing_key_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The phase-aware verdict is provider-neutral: openai on run_task fails hard."""
    _full_anthropic_env(monkeypatch)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY is not set"):
        Config.check_secrets(
            provider="anthropic", phase_providers={"run_task": "openai"}
        )


def test_phase_openai_nonmutating_missing_key_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai routed only to a non-mutating phase degrades to a warning."""
    _full_anthropic_env(monkeypatch)
    warnings = Config.check_secrets(
        provider="anthropic", phase_providers={"contemplation": "openai"}
    )
    matching = [w for w in warnings if "OPENAI_API_KEY" in w]
    assert len(matching) == 1
    assert "contemplation" in matching[0]


def test_phase_openai_nonmutating_bad_prefix_warning_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed per-phase credential surfaces its normal prefix warning."""
    _full_anthropic_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "bad-prefix-key")
    warnings = Config.check_secrets(
        provider="anthropic", phase_providers={"contemplation": "openai"}
    )
    assert any("OPENAI_API_KEY has unexpected prefix" in w for w in warnings)


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = ""
) -> None:
    """Patch ``config.subprocess.run`` to return a canned CompletedProcess."""
    def _run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["claude"], returncode=returncode, stdout=stdout, stderr=""
        )
    monkeypatch.setattr(config_module.subprocess, "run", _run)


def test_claude_cli_logged_in_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean exit reporting ``loggedIn: true`` returns True."""
    _fake_run(monkeypatch, returncode=0, stdout='{"loggedIn": true}')
    assert Config._claude_cli_logged_in() is True


def test_claude_cli_logged_in_false_when_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean exit reporting ``loggedIn: false`` returns False."""
    _fake_run(monkeypatch, returncode=0, stdout='{"loggedIn": false}')
    assert Config._claude_cli_logged_in() is False


def test_claude_cli_logged_in_false_when_field_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload without ``loggedIn`` returns False (not zero-fill to truthy)."""
    _fake_run(monkeypatch, returncode=0, stdout='{"authMethod": "claude.ai"}')
    assert Config._claude_cli_logged_in() is False


def test_claude_cli_logged_in_false_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit returns False regardless of stdout."""
    _fake_run(monkeypatch, returncode=1, stdout='{"loggedIn": true}')
    assert Config._claude_cli_logged_in() is False


def test_claude_cli_logged_in_false_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-JSON stdout returns False."""
    _fake_run(monkeypatch, returncode=0, stdout="not json at all")
    assert Config._claude_cli_logged_in() is False


def test_claude_cli_logged_in_false_when_payload_not_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON value that is not an object returns False."""
    _fake_run(monkeypatch, returncode=0, stdout="[1, 2, 3]")
    assert Config._claude_cli_logged_in() is False


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("claude"),
        subprocess.TimeoutExpired(cmd="claude", timeout=10),
        OSError("boom"),
    ],
)
def test_claude_cli_logged_in_false_on_subprocess_error(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Probe subprocess failures convert to a False (not-logged-in) verdict."""
    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise exc
    monkeypatch.setattr(config_module.subprocess, "run", _raise)
    assert Config._claude_cli_logged_in() is False


def test_check_secrets_unknown_provider_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown provider name fails loud with the valid-names remediation."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    with pytest.raises(SystemExit, match="Unknown SDK provider"):
        Config.check_secrets(provider="bogus")


# =====================================================================
# check_secrets — proposal 039 GITHUB_TOKEN scope soft-check
# =====================================================================


def _publication_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standard env for the publication soft-check tests."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "tok")


def test_check_secrets_publication_warns_when_repo_scope_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When publication_enabled=true and gh auth status omits 'repo',
    a WARNING log is emitted referencing proposal 039."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="",
            stderr="Logged in as user. Token scopes: 'gist', 'workflow'",
        )

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    matches = [r for r in caplog.records if "proposal 039" in r.getMessage()]
    assert len(matches) == 1
    assert "repo" in matches[0].getMessage()


def test_check_secrets_publication_no_warn_when_repo_scope_present(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When subprocess.run returns text containing 'repo', no warning."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="",
            stderr="Logged in as user. Token scopes: 'repo', 'workflow'",
        )

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    assert not any("proposal 039" in r.getMessage() for r in caplog.records)


def test_check_secrets_publication_warns_when_only_public_repo_scope(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Token with only 'public_repo' (insufficient for `gh repo create` of
    private repos) must still trigger the warning. The previous substring
    check `"repo" in ...` matched 'public_repo' and silently passed —
    defeating the soft-check. The fix uses `"'repo'"` (quoted) so only the
    exact `repo` scope satisfies it."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="",
            stderr="Logged in as user. Token scopes: 'gist', 'public_repo', 'workflow'",
        )

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    matches = [r for r in caplog.records if "proposal 039" in r.getMessage()]
    assert len(matches) == 1, (
        "Token with only 'public_repo' scope should trigger the soft-warning; "
        "substring check that matches 'public_repo' is a false negative."
    )


def test_check_secrets_publication_no_warn_when_repo_scope_double_quoted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Forward-compat: a future `gh` that switches to double quotes around
    scopes must not trip the soft-check on a properly-scoped token."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="",
            stderr='Logged in as user. Token scopes: "repo", "workflow"',
        )

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    assert not any("proposal 039" in r.getMessage() for r in caplog.records)


def test_check_secrets_publication_no_warn_when_repo_scope_unquoted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Forward-compat: a future `gh` that drops quotes entirely around scopes
    must not trip the soft-check on a properly-scoped token."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="",
            stderr="Logged in as user. Token scopes: repo, workflow",
        )

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    assert not any("proposal 039" in r.getMessage() for r in caplog.records)


def test_check_secrets_publication_warns_when_only_repo_admin_scope(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Negative regression: a substring-style match that drops to
    `re.search("repo", …)` (no word boundary) would silently pass on
    `repo_admin` because `repo` is a prefix of it. Word-boundary matching
    must reject `repo_admin` (the trailing `_` is a word character, so the
    right boundary is absent) and still fire the warning."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="",
            stderr="Logged in as user. Token scopes: 'gist', 'repo_admin', 'workflow'",
        )

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    matches = [r for r in caplog.records if "proposal 039" in r.getMessage()]
    assert len(matches) == 1, (
        "Token with only 'repo_admin' scope should trigger the soft-warning; "
        "a substring check that matches 'repo_admin' is a false negative."
    )


@pytest.mark.parametrize("subscope", ["repo:status", "repo:invite"])
def test_check_secrets_publication_warns_when_only_repo_subscope(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    subscope: str,
) -> None:
    """Negative regression (#1842): GitHub OAuth exposes narrower
    colon-delimited siblings of `repo` (`repo:status`, `repo:invite`,
    `repo:deployment`) that do NOT confer the privileges `gh repo create`
    needs. A bare `\\brepo\\b` match silently passed on these because `:`
    is a non-word character, so `\\b` finds a boundary right after `repo`.
    The tightened `\\brepo(?![\\w:])` must reject the sub-scopes and fire
    the warning."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="",
            stderr=f"Logged in as user. Token scopes: 'gist', '{subscope}', 'workflow'",
        )

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    matches = [r for r in caplog.records if "proposal 039" in r.getMessage()]
    assert len(matches) == 1, (
        f"Token with only '{subscope}' scope should trigger the soft-warning; "
        f"matching '{subscope}' as full 'repo' is a false negative."
    )


def test_check_secrets_publication_swallows_subprocess_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When subprocess.run raises (e.g. FileNotFoundError, TimeoutExpired),
    no warning, no exception, function returns normally."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        warnings = Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    assert isinstance(warnings, list)
    assert not any("proposal 039" in r.getMessage() for r in caplog.records)


def test_check_secrets_publication_swallows_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When subprocess.run raises TimeoutExpired (gh probe exceeded timeout=5),
    no warning, no exception, function returns normally."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=5)

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        warnings = Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    assert isinstance(warnings, list)
    assert not any("proposal 039" in r.getMessage() for r in caplog.records)


def test_check_secrets_publication_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When subprocess.run raises an OSError subclass (e.g. PermissionError),
    no warning, no exception, function returns normally."""
    _publication_env(monkeypatch)

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise PermissionError("denied")

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        warnings = Config.check_secrets(publication_enabled=True, check_publication_scope=True)

    assert isinstance(warnings, list)
    assert not any("proposal 039" in r.getMessage() for r in caplog.records)


def test_check_secrets_publication_disabled_skips_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When publication_enabled=False, subprocess.run is not called at all."""
    _publication_env(monkeypatch)

    calls: list[Any] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    Config.check_secrets(publication_enabled=False)
    assert calls == []


def test_check_secrets_publication_smoke_skips_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In smoke mode the soft-check is skipped even with publication_enabled=True."""
    _publication_env(monkeypatch)

    calls: list[Any] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)
    Config.check_secrets(publication_enabled=True, smoke=True)
    assert calls == []


def test_check_secrets_publication_skips_probe_when_scope_flag_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #1601: short-lived CLI commands (`run`, `contemplate`, `evolve`,
    `serve`) build the agent through `_build_agent`, which calls
    `check_secrets(publication_enabled=config.publication_enabled)` without the
    scope flag. With `publication_enabled=true` (the default), the proposal-039
    probe used to fire on every invocation, paying a 5-second `gh`-not-on-PATH
    timeout. The fix gates the probe behind `check_publication_scope=True`,
    set only by `cmd_loop` and `cmd_preflight`. This test proves the gate
    short-circuits before `subprocess.run` is reached when the scope flag is
    False (or omitted, which defaults to False)."""
    _publication_env(monkeypatch)

    fake_run = Mock(side_effect=AssertionError("subprocess.run must not be called"))
    monkeypatch.setattr("maestro.config.subprocess.run", fake_run)

    with caplog.at_level(logging.WARNING, logger="maestro.config"):
        Config.check_secrets(publication_enabled=True, check_publication_scope=False)
        Config.check_secrets(publication_enabled=True)

    assert fake_run.call_count == 0
    assert not any("proposal 039" in r.getMessage() for r in caplog.records)


# =====================================================================
# _read_toml / external-section registry
# =====================================================================


def test_external_sections_whitelist_is_module_level() -> None:
    """The external-section whitelist is a module-level constant, not in-body."""
    whitelist = toml_config._KNOWN_EXTERNAL_SECTIONS
    assert whitelist == frozenset({"review"})


def test_read_toml_missing_file_returns_empty(mem_fs: dict[str, str]) -> None:
    """_read_toml swallows FileNotFoundError and returns an empty dict."""
    from maestro.toml_config import _read_toml

    missing = Path("/fake/does_not_exist.toml")
    assert _read_toml(missing) == {}


def test_read_toml_malformed_warns_and_returns_empty(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """_read_toml swallows a parse error, warns, and returns an empty dict."""
    from maestro.toml_config import _read_toml

    path = Path("/fake/bad.toml")
    mem_fs[str(path)] = "this is = not [valid toml"
    with caplog.at_level(logging.WARNING, logger="maestro"):
        assert _read_toml(path) == {}
    assert "Failed to read or parse TOML config" in caplog.text


# =====================================================================
# _deep_merge
# =====================================================================


def test_deep_merge() -> None:
    base = {"timeouts": {"git": 30, "gh": 30}, "dashboard": {"port": 8420}}
    overlay = {"timeouts": {"git": 60}, "loop": {"interval": 10}}
    result = _deep_merge(base, overlay)
    assert result == {
        "timeouts": {"git": 60, "gh": 30},
        "dashboard": {"port": 8420},
        "loop": {"interval": 10},
    }


def test_deep_merge_overlay_list_not_shared() -> None:
    """Mutating overlay after merge must not affect the merged result."""
    base: dict[str, Any] = {"section": {"key": [1, 2]}}
    overlay: dict[str, Any] = {"section": {"key": [3, 4]}}
    merged = _deep_merge(base, overlay)
    overlay["section"]["key"].append(999)
    assert merged["section"]["key"] == [3, 4]


def test_deep_merge_base_dict_not_shared() -> None:
    """Mutating base after merge must not affect the merged result."""
    base: dict[str, Any] = {"section": {"a": 1}}
    overlay: dict[str, Any] = {"other": 2}
    merged = _deep_merge(base, overlay)
    base["section"]["a"] = 999
    assert merged["section"]["a"] == 1


# =====================================================================
# Committed maestro.toml defaults (via Config dataclass defaults)
# =====================================================================


def test_duplicate_pending_plan_threshold_default() -> None:
    """The duplicate-pending-plan guard loads with its documented default."""
    config = Config()
    assert config.loop_duplicate_pending_plan_threshold == 0.5


def test_pending_plan_cluster_threshold_default() -> None:
    """The cluster-level themed-restatement guard loads its documented default."""
    config = Config()
    assert config.loop_pending_plan_cluster_threshold == 2


def test_pending_cluster_gate_defaults() -> None:
    """The cluster-level pending-plan stop loads with documented defaults."""
    config = Config()
    assert config.loop_pending_cluster_threshold == 0.35
    assert config.loop_pending_cluster_max == 2
    # The mechanical sweep ships enabled by default.
    assert config.loop_pending_cluster_sweep_enabled is True


def test_restatement_sweep_defaults_on() -> None:
    """The pending-plan restatement sweep ships enabled by default."""
    config = Config()
    assert config.loop_restatement_sweep_enabled is True


def test_pending_plan_submit_block_threshold_default() -> None:
    """The backlog-saturation submit-block ships with a default cap of 5."""
    config = Config()
    assert config.loop_pending_plan_submit_block_threshold == 5


def test_plan_review_expiry_days_default() -> None:
    """The pending-plan expiry horizon ships with a default of 21 days."""
    config = Config()
    assert config.loop_plan_review_expiry_days == 21


def test_branch_resolved_gate_defaults() -> None:
    """The costume-signature gate config loads with documented defaults."""
    config = Config()
    assert config.branch_resolved_gate_enabled is True
    assert config.branch_resolved_repetition_threshold == 2


def test_merge_block_local_probe_default() -> None:
    """The conflict-aware merge-block probe is on by default."""
    config = Config()
    assert config.merge_block_local_probe_enabled is True


@patch("dotenv.load_dotenv")
def test_from_env_uses_default_pricing_when_toml_omits_section(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """from_env() returns populated sdk_pricing from the dataclass default
    when TOML has no [sdk.pricing.*] tables."""
    # Strip all [sdk.pricing.*] sections from the base TOML so the loader
    # never populates sdk_pricing — from_env() should fall back to the
    # dataclass default_factory.
    import re as _re

    # ``[^\n]+\]\n`` (rather than ``[^\]]+``) tolerates ``]`` characters
    # inside quoted section names like ``[sdk.pricing."claude-opus-4-7[1m]"]``
    # — the actual closing bracket is the last one on the line.
    stripped = _re.sub(
        r'\[sdk\.pricing\.[^\n]+\]\n(?:[^\[]*\n)*', '', _BASE_TOML,
    )
    mem_fs["/fake/toml/maestro.toml"] = stripped
    config = Config.from_env(config_dir=Path("/fake/toml"))
    # The default_factory should have provided built-in pricing
    default_pricing = Config().sdk_pricing
    assert config.sdk_pricing == default_pricing
    assert len(config.sdk_pricing) > 0


def test_github_issues_page_size_accepts_one() -> None:
    """Minimum valid page size is 1."""
    config = Config(github_issues_page_size=1)
    config.validate()


def test_max_implementations_per_iteration_defaults_to_one() -> None:
    """The default cap is 1 — bounds blast radius when the queue accumulates."""
    config = Config()
    config.validate()
    assert config.max_implementations_per_iteration == 1


def test_audit_stale_entry_enabled_defaults_to_true() -> None:
    """Default-on — rule 6 fires in every audit_self_model cycle."""
    config = Config()
    assert config.audit_stale_entry_enabled is True


def test_orphaned_cure_retire_validation_accepts_boundary() -> None:
    """A ceiling exactly one above the flag threshold is the minimum accepted."""
    Config(
        orphaned_cure_age_cycles=3,
        orphaned_cure_retire_age_cycles=4,
    ).validate()


def test_config_phantom_resolution_keys_retired(
    mem_fs: dict[str, str],
) -> None:
    """The phantom-resolution audit rule (rule 13) was retired (proposal 105):
    the record-time outcome authority already forces an unlanded landing claim
    to ``partial``, so the post-hoc detector caught nothing. All four
    ``phantom_resolution_*`` ``[audit]`` flags are gone from ``Config`` and none
    round-trips through the spec — a stray occurrence is now rejected by the
    declarative parser's ``extra="forbid"`` guard, not accepted. The still-present
    ``phantom_escalation_delivery_enabled`` control proves the ``hasattr`` probe is
    not vacuously true (the stale-field-drop idiom)."""
    config = Config()
    # Control: a sibling forced-arm [audit] flag that stays, so the negative
    # checks below cannot pass merely because ``hasattr`` is broken here.
    assert hasattr(config, "phantom_escalation_delivery_enabled")
    assert not hasattr(config, "phantom_resolution_enabled")
    assert not hasattr(config, "phantom_resolution_age_cycles")
    assert not hasattr(config, "phantom_resolution_forced_enabled")
    assert not hasattr(config, "phantom_resolution_ready_only_enabled")
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "[audit]\n",
        "[audit]\nphantom_resolution_enabled = true\n"
        "phantom_resolution_age_cycles = 3\n"
        "phantom_resolution_forced_enabled = true\n"
        "phantom_resolution_ready_only_enabled = true\n",
        1,
    )
    with pytest.raises(ValidationError):
        build_config_via_pydantic(Path("/fake/toml"))


def test_self_model_external_anchor_default_off(
    mem_fs: dict[str, str],
) -> None:
    """Committed default for the proposal-120 self-model external-capability
    anchor (Phase C) loads from the base TOML and agrees with the dataclass
    default (default-off switch)."""
    mem_fs["/fake/config/maestro.toml"] = _BASE_TOML
    built = build_config_via_pydantic(Path("/fake/config"))
    assert built.self_model_external_anchor_enabled is False
    cfg = Config()
    assert cfg.self_model_external_anchor_enabled is False
    cfg.validate()


def test_self_model_external_anchor_override_from_toml(
    mem_fs: dict[str, str],
) -> None:
    """A dev overlay flips the external-capability anchor on and it flows
    through."""
    mem_fs["/fake/config/maestro.toml"] = _BASE_TOML
    mem_fs["/fake/config/maestro.dev.toml"] = (
        "[loop]\n"
        "self_model_external_anchor_enabled = true\n"
    )
    built = build_config_via_pydantic(Path("/fake/config"), env_name="dev")
    assert built.self_model_external_anchor_enabled is True
    cfg = Config(
        self_model_external_anchor_enabled=(
            built.self_model_external_anchor_enabled
        ),
    )
    cfg.validate()
    assert cfg.self_model_external_anchor_enabled is True


def test_landing_credit_canary_flags_resolve_through_overlay(
    mem_fs: dict[str, str],
) -> None:
    """Issue #3280's landing-credit flags reach ``Config`` through the overlay path.

    The overlay-resolution complement of the committed-file guards in
    ``test_config_flag_lifecycle.py``: those assert the real ``maestro.toml`` /
    ``maestro.production.toml`` text resolves correctly, while this exercises the
    merge machinery itself on a synthetic in-memory overlay — a base load leaves all
    three off, and an overlay setting them across *two different sections*
    (``[audit]`` for the two rules, ``[loop]`` for the write-time gate) flows every
    one of them through to the built ``Config`` and survives ``validate()``. The
    split across sections is what makes this non-redundant: a section-routing bug
    would land the ``[loop]`` key silently unapplied while the ``[audit]`` pair
    still worked.
    """
    mem_fs["/fake/config/maestro.toml"] = _BASE_TOML
    base_built = build_config_via_pydantic(Path("/fake/config"))
    assert base_built.undercredited_landing_enabled is False
    assert base_built.reconcile_near_miss_enabled is False
    assert base_built.prestamp_merge_truth_gate_enabled is False

    mem_fs["/fake/config/maestro.prod-like.toml"] = (
        "[audit]\n"
        "undercredited_landing_enabled = true\n"
        "reconcile_near_miss_enabled = true\n"
        "[loop]\n"
        "prestamp_merge_truth_gate_enabled = true\n"
    )
    built = build_config_via_pydantic(Path("/fake/config"), env_name="prod-like")
    assert built.undercredited_landing_enabled is True
    assert built.reconcile_near_miss_enabled is True
    assert built.prestamp_merge_truth_gate_enabled is True
    # The near-band ceiling is deliberately not overridden — it inherits base.
    assert built.reconcile_near_max_behind == base_built.reconcile_near_max_behind
    built.validate()


def test_owned_act_drought_flag_gates_registry() -> None:
    """Disabling the flag suppresses rule 18 from ``_registered_audit_checks``;
    the default-on path includes it."""
    from maestro import audit_checks

    enabled = {
        fn.__name__ for fn in audit_checks._registered_audit_checks()
    }
    assert "check_owned_act_drought" in enabled

    disabled = {
        fn.__name__
        for fn in audit_checks._registered_audit_checks(
            owned_act_drought_enabled=False,
        )
    }
    assert "check_owned_act_drought" not in disabled
    # Disabling only removes rule 18 — every other rule survives.
    assert enabled - disabled == {"check_owned_act_drought"}


def test_pr_remediation_lap_budget_default() -> None:
    """The per-PR lap budget defaults to 6 (proposal 080 Phase 3)."""
    config = Config()
    assert config.pr_remediation_lap_budget == 6


def test_pr_remediation_lap_budget_zero_disables_is_valid() -> None:
    """A budget of 0 is permitted — it disables the lap-budget standdown."""
    Config(pr_remediation_lap_budget=0).validate()


def test_sdk_rate_limit_idle_max_below_probe_interval_rejected() -> None:
    """Max probe interval must be >= probe interval."""
    config = Config(
        sdk_rate_limit_idle_probe_interval=300,
        sdk_rate_limit_idle_max_probe_interval=100,
    )
    with pytest.raises(
        ValueError,
        match="sdk_rate_limit_idle_max_probe_interval must be >= "
        "sdk_rate_limit_idle_probe_interval",
    ):
        config.validate()


def test_zero_startup_rate_limit_cooldown_accepted() -> None:
    """Zero is the disabled sentinel and must validate."""
    config = Config(startup_rate_limit_cooldown_seconds=0)
    config.validate()


def test_sdk_max_concurrent_queries_default() -> None:
    """Default value for sdk_max_concurrent_queries is 2."""
    config = Config()
    assert config.sdk_max_concurrent_queries == 2


def test_sdk_single_use_evolution_calls_default_off() -> None:
    """Proposal 139's single-use routing ships default-off."""
    assert Config().sdk_single_use_evolution_calls_enabled is False


def test_sdk_single_use_evolution_calls_from_toml(mem_fs: dict[str, str]) -> None:
    """``[sdk] single_use_evolution_calls_enabled`` round-trips through from_env."""
    old = "single_use_evolution_calls_enabled = false"
    assert _BASE_TOML.count(old) == 1

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert config.sdk_single_use_evolution_calls_enabled is False

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        old, "single_use_evolution_calls_enabled = true",
    )
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert config.sdk_single_use_evolution_calls_enabled is True


def test_sdk_call_policy_resolution_default_off() -> None:
    """Proposal 140's seam effort resolution ships default-off."""
    assert Config().sdk_call_policy_resolution_enabled is False


def test_sdk_enforced_tool_surface_default_off() -> None:
    """Proposal 142's seam tool-surface enforcement ships default-off."""
    assert Config().sdk_enforced_tool_surface_enabled is False


def test_sdk_enforced_tool_surface_from_toml(mem_fs: dict[str, str]) -> None:
    """``[sdk] enforced_tool_surface_enabled`` round-trips from TOML."""
    old = "enforced_tool_surface_enabled = false"
    assert _BASE_TOML.count(old) == 1

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert config.sdk_enforced_tool_surface_enabled is False

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        old, "enforced_tool_surface_enabled = true",
    )
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert config.sdk_enforced_tool_surface_enabled is True


def test_sdk_call_policy_resolution_from_toml(mem_fs: dict[str, str]) -> None:
    """``[sdk] call_policy_resolution_enabled`` round-trips with its prerequisites."""
    old = "call_policy_resolution_enabled = false"
    assert _BASE_TOML.count(old) == 1

    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert config.sdk_call_policy_resolution_enabled is False

    mem_fs["/fake/toml/maestro.toml"] = (
        _BASE_TOML.replace(old, "call_policy_resolution_enabled = true")
        .replace(
            "single_use_evolution_calls_enabled = false",
            "single_use_evolution_calls_enabled = true",
        )
        .replace(
            "self_review_own_session_enabled = false",
            "self_review_own_session_enabled = true",
        )
    )
    config = Config.from_env(env_file=None, config_dir=Path("/fake/toml"))
    assert config.sdk_call_policy_resolution_enabled is True


@pytest.mark.parametrize(
    "missing",
    ["sdk_single_use_evolution_calls_enabled", "self_review_own_session_enabled"],
)
def test_sdk_call_policy_resolution_requires_prerequisites(missing: str) -> None:
    """The flag refuses to load unless both prerequisite flags are on."""
    config = Config(
        sdk_call_policy_resolution_enabled=True,
        sdk_single_use_evolution_calls_enabled=True,
        self_review_own_session_enabled=True,
    )
    setattr(config, missing, False)
    with pytest.raises(ValueError, match=missing):
        config.validate()


def test_grounded_planning_requires_single_use_evolution_calls() -> None:
    """Proposal 143: the flag refuses to load without the single-use arm."""
    config = Config(grounded_planning_enabled=True)
    with pytest.raises(
        ValueError, match="sdk_single_use_evolution_calls_enabled",
    ):
        config.validate()


def test_grounded_planning_loads_with_single_use_evolution_calls() -> None:
    Config(
        grounded_planning_enabled=True,
        sdk_single_use_evolution_calls_enabled=True,
    ).validate()


def test_config_grounded_planning_committed_toml_default_off() -> None:
    assert _committed_toml_vals()["grounded_planning_enabled"] is False


def test_config_grounded_planning_rejection_tool_default_off() -> None:
    assert Config().grounded_planning_rejection_tool_enabled is False
    assert (
        _committed_toml_vals()["grounded_planning_rejection_tool_enabled"] is False
    )


def test_sdk_call_policy_resolution_loads_with_prerequisites() -> None:
    """Both prerequisites on → the flag validates."""
    Config(
        sdk_call_policy_resolution_enabled=True,
        sdk_single_use_evolution_calls_enabled=True,
        self_review_own_session_enabled=True,
    ).validate()


# =====================================================================
# Standby window
# =====================================================================


def test_standby_defaults_disabled() -> None:
    """Standby is disabled by default with 13:00 - 19:00 UTC window."""
    config = Config()
    assert config.standby_enabled is False
    assert config.standby_start_hour_utc == 13
    assert config.standby_end_hour_utc == 19


def test_validate_rejects_equal_standby_hours() -> None:
    """Equal start/end would mean always-on or always-off — ambiguous."""
    config = Config(standby_start_hour_utc=10, standby_end_hour_utc=10)
    with pytest.raises(ValueError, match="must differ"):
        config.validate()


def test_validate_accepts_wraparound_standby() -> None:
    """start > end (window crossing midnight UTC) is allowed."""
    config = Config(standby_start_hour_utc=22, standby_end_hour_utc=2)
    config.validate()  # does not raise


def test_validate_accepts_standby_boundary_hours() -> None:
    """0 and 23 are valid hour values."""
    config = Config(standby_start_hour_utc=0, standby_end_hour_utc=23)
    config.validate()  # does not raise


@patch("dotenv.load_dotenv")
def test_config_from_env_standby(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """Standby fields are wired through from_env()."""
    toml = _BASE_TOML.replace(
        "standby_enabled = false", "standby_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.standby_enabled is True
    assert config.standby_start_hour_utc == 13
    assert config.standby_end_hour_utc == 19


# =====================================================================
# Peak-hours scheduled budget throttle
# =====================================================================


def test_peak_throttle_dataclass_defaults_disabled() -> None:
    """``Config()`` defaults match committed maestro.toml: 5-11 AM PT, off."""
    config = Config()
    assert config.peak_throttle_enabled is False
    assert config.peak_throttle_timezone == "America/Los_Angeles"
    assert config.peak_throttle_start_hour == 5
    assert config.peak_throttle_end_hour == 11
    assert config.peak_throttle_weekdays_only is True
    assert config.peak_throttle_rate_multiplier == 0.5


def test_validate_accepts_valid_peak_throttle_rate_multiplier() -> None:
    """A positive factor (tightening or loosening) passes validation."""
    Config(peak_throttle_rate_multiplier=0.5).validate()
    Config(peak_throttle_rate_multiplier=2.0).validate()


def test_validate_rejects_empty_peak_throttle_timezone() -> None:
    config = Config(peak_throttle_timezone="   ")
    with pytest.raises(ValueError, match="peak_throttle_timezone"):
        config.validate()


def test_validate_rejects_non_string_peak_throttle_timezone() -> None:
    config = Config(peak_throttle_timezone=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="peak_throttle_timezone"):
        config.validate()


def test_validate_rejects_unknown_peak_throttle_timezone() -> None:
    config = Config(peak_throttle_timezone="Not/A/Real/Zone")
    with pytest.raises(ValueError, match="peak_throttle_timezone"):
        config.validate()


def test_validate_accepts_iana_peak_throttle_timezone() -> None:
    Config(peak_throttle_timezone="America/Los_Angeles").validate()
    Config(peak_throttle_timezone="UTC").validate()
    Config(peak_throttle_timezone="Europe/London").validate()


def test_validate_rejects_equal_peak_throttle_hours() -> None:
    config = Config(
        peak_throttle_start_hour=10, peak_throttle_end_hour=10,
    )
    with pytest.raises(ValueError, match="must differ"):
        config.validate()


def test_validate_accepts_wraparound_peak_throttle() -> None:
    config = Config(
        peak_throttle_start_hour=22, peak_throttle_end_hour=2,
    )
    config.validate()  # does not raise


def test_validate_accepts_peak_throttle_boundary_hours() -> None:
    config = Config(
        peak_throttle_start_hour=0, peak_throttle_end_hour=23,
    )
    config.validate()  # does not raise


@patch("dotenv.load_dotenv")
def test_config_from_env_peak_throttle(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """Peak-throttle fields are wired through from_env()."""
    toml = _BASE_TOML.replace(
        "peak_throttle_enabled = false", "peak_throttle_enabled = true",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.peak_throttle_enabled is True
    assert config.peak_throttle_timezone == "America/Los_Angeles"
    assert config.peak_throttle_start_hour == 5
    assert config.peak_throttle_end_hour == 11
    assert config.peak_throttle_weekdays_only is True


# =====================================================================
# SDK fallback model, task budget
# =====================================================================


@patch("dotenv.load_dotenv")
def test_config_from_env_rejects_retired_thinking_budget_key(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """The retired ``[sdk] thinking_budget_tokens`` key fails config load
    (proposal 140) — ``extra="forbid"`` on ``_SdkSettings`` refuses it and
    ``from_env`` re-raises that as ``ValueError``."""
    toml = _BASE_TOML.replace(
        "task_budget_tokens = 0", "thinking_budget_tokens = 10000\ntask_budget_tokens = 0", 1,
    )
    assert "\nthinking_budget_tokens = 10000" in toml
    mem_fs["/fake/toml/maestro.toml"] = toml
    with pytest.raises(ValueError, match="thinking_budget_tokens"):
        Config.from_env(config_dir=Path("/fake/toml"))


def test_sdk_task_budget_tokens_default() -> None:
    config = Config()
    assert config.sdk_task_budget_tokens == 0


def test_sdk_task_budget_tokens_zero_valid() -> None:
    Config(sdk_task_budget_tokens=0).validate()


def test_sdk_task_budget_tokens_valid_above_min() -> None:
    Config(sdk_task_budget_tokens=1024).validate()
    Config(sdk_task_budget_tokens=100_000).validate()


def test_sdk_task_budget_tokens_rejects_low_positive() -> None:
    """Values 1-1023 are rejected (too small to be useful)."""
    config = Config(sdk_task_budget_tokens=500)
    with pytest.raises(ValueError, match=r"sdk_task_budget_tokens.*1024"):
        config.validate()


def test_sdk_task_budget_tokens_rejects_bool() -> None:
    config = Config(sdk_task_budget_tokens=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sdk_task_budget_tokens"):
        config.validate()


def test_sdk_fallback_model_default() -> None:
    config = Config()
    assert config.sdk_fallback_model == ""


@patch("dotenv.load_dotenv")
def test_config_from_env_sdk_extras(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    """New SDK fields are wired through from_env()."""
    toml = _BASE_TOML.replace(
        'fallback = ""', 'fallback = "claude-sonnet-4-6"',
    ).replace(
        "task_budget_tokens = 0", "task_budget_tokens = 50000",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.sdk_fallback_model == "claude-sonnet-4-6"
    assert config.sdk_task_budget_tokens == 50000


# Burn-rate standby gate validation


def test_max_loop_budget_usd_per_hour_default() -> None:
    assert Config().max_loop_budget_usd_per_hour == 0.0


def test_max_loop_budget_usd_per_hour_accepts_zero() -> None:
    Config(max_loop_budget_usd_per_hour=0.0).validate()


def test_max_loop_budget_usd_per_hour_accepts_positive() -> None:
    Config(max_loop_budget_usd_per_hour=5.0).validate()


def test_max_loop_budget_usd_per_hour_accepts_int_coerced() -> None:
    Config(max_loop_budget_usd_per_hour=10).validate()  # type: ignore[arg-type]


def test_budget_rate_window_hours_default() -> None:
    assert Config().budget_rate_window_hours == 1.0


def test_budget_rate_window_hours_accepts_positive() -> None:
    Config(budget_rate_window_hours=0.5).validate()
    Config(budget_rate_window_hours=24).validate()  # type: ignore[arg-type]


def test_budget_rate_min_cost_usd_default() -> None:
    assert Config().budget_rate_min_cost_usd == 0.10


def test_budget_rate_min_cost_usd_accepts_zero() -> None:
    Config(budget_rate_min_cost_usd=0.0).validate()


def test_budget_rate_min_cost_usd_accepts_positive() -> None:
    Config(budget_rate_min_cost_usd=2.5).validate()


def test_budget_throttle_max_multiplier_default() -> None:
    assert Config().budget_throttle_max_multiplier == 60.0


def test_budget_throttle_max_multiplier_accepts_one() -> None:
    Config(budget_throttle_max_multiplier=1.0).validate()


def test_budget_throttle_max_multiplier_accepts_large() -> None:
    Config(budget_throttle_max_multiplier=300.0).validate()
    Config(budget_throttle_max_multiplier=10).validate()  # type: ignore[arg-type]


@patch("dotenv.load_dotenv")
def test_config_from_env_burn_rate_keys(
    mock_load: object, mem_fs: dict[str, str],
) -> None:
    toml = _BASE_TOML.replace(
        "max_loop_budget_usd_per_hour = 0.0",
        "max_loop_budget_usd_per_hour = 3.50",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.max_loop_budget_usd_per_hour == 3.50


def test_sdk_stream_idle_timeout_seconds_default() -> None:
    config = Config()
    assert config.sdk_stream_idle_timeout_seconds == 180.0


def test_sdk_stream_idle_timeout_seconds_zero_disables() -> None:
    Config(sdk_stream_idle_timeout_seconds=0).validate()


def test_sdk_stream_idle_timeout_seconds_negative_disables() -> None:
    Config(sdk_stream_idle_timeout_seconds=-1).validate()


def test_sdk_tool_stream_idle_timeout_seconds_default() -> None:
    config = Config()
    assert config.sdk_tool_stream_idle_timeout_seconds == 1800.0


def test_sdk_tool_stream_idle_timeout_seconds_zero_disables() -> None:
    Config(sdk_tool_stream_idle_timeout_seconds=0).validate()


def test_sdk_tool_stream_idle_timeout_seconds_negative_disables() -> None:
    Config(sdk_tool_stream_idle_timeout_seconds=-1).validate()


def test_sdk_tool_stream_idle_timeout_seconds_roundtrips_from_toml(
    mem_fs: dict[str, str],
) -> None:
    toml = _BASE_TOML.replace(
        "tool_stream_idle_timeout_seconds = 1800",
        "tool_stream_idle_timeout_seconds = 2400",
    )
    mem_fs["/fake/toml/maestro.toml"] = toml
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.sdk_tool_stream_idle_timeout_seconds == 2400.0


# =====================================================================
# Artifact review + publication — proposal 030 config scaffolding
# =====================================================================


def test_config_artifact_review_defaults() -> None:
    config = Config()
    assert config.artifact_review_enabled is True
    assert config.artifact_review_every == 20
    assert config.artifact_review_batch_size == 3
    assert config.artifact_review_max_file_bytes == 4096
    assert config.artifact_review_max_files_per_task == 10
    assert config.artifact_review_max_bundle_bytes == 12288


def test_config_publication_defaults() -> None:
    config = Config()
    # Proposal 032 flipped ``enabled`` to True — the dataclass default
    # now matches the committed maestro.toml so the approval surface
    # activates without an overlay.
    assert config.publication_enabled is True
    # Publications are created under the configured GitHub namespace.
    assert config.publication_owner == "thomas-sodon"
    assert config.publication_copyright_holder == "Thomas Sodon"
    assert config.publication_license == "MIT"
    assert config.publication_secret_scan_enabled is True
    assert config.publication_executing_stale_timeout_seconds == 600
    assert config.publication_push_retry_limit == 3


def test_config_accepts_minute_executing_stale_timeout_seconds() -> None:
    Config(publication_executing_stale_timeout_seconds=60).validate()


def test_config_accepts_empty_publication_owner() -> None:
    """Empty owner is the sentinel for 'derive at 032 startup'."""
    Config(publication_owner="").validate()


@patch("dotenv.load_dotenv")
def test_config_from_env_applies_artifact_overlay(
    mock_load: object,
    mem_fs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAESTRO_ENV overlay can override artifact/publication keys."""
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML
    mem_fs["/fake/toml/maestro.dev.toml"] = (
        "[artifacts]\n"
        "review_enabled = true\n"
        "review_every = 7\n"
        "[publication]\n"
        "enabled = true\n"
        'owner = "overlay-owner"\n'
        "push_retry_limit = 5\n"
    )
    monkeypatch.setenv("MAESTRO_ENV", "dev")
    config = Config.from_env(config_dir=Path("/fake/toml"))
    assert config.artifact_review_enabled is True
    assert config.artifact_review_every == 7
    assert config.publication_enabled is True
    assert config.publication_owner == "overlay-owner"
    assert config.publication_push_retry_limit == 5


# ---------------------------------------------------------------------------
# archive_threshold_days — tiered-memory rotation knob
# ---------------------------------------------------------------------------


def test_archive_threshold_days_default_is_disabled() -> None:
    assert Config().archive_threshold_days == 0


def test_archive_threshold_days_accepts_zero() -> None:
    Config(archive_threshold_days=0).validate()


def test_archive_threshold_days_inside_hot_window_rejected() -> None:
    """7 days sits inside the widest hot read, not above it.

    A threshold whose hours do not *exceed* the widest hot read sheds
    still-in-window ``cost_samples.jsonl`` rows to cold storage, so the
    dashboard's budget / cost-rate view under-reports spend.
    """
    with pytest.raises(
        ValueError, match=r"archive_threshold_days must be 0 \(disabled\)"
    ):
        Config(archive_threshold_days=7).validate()


def test_archive_threshold_days_above_floor_accepted() -> None:
    """The plan-review expiry floor (21 days) binds, so 22 days clears it."""
    Config(archive_threshold_days=22).validate()



def test_archive_threshold_days_raised_burn_rate_window_lifts_floor() -> None:
    """A ``budget_rate_window_hours`` above the other windows lifts the floor."""
    with pytest.raises(ValueError, match="at least 42 days"):
        Config(
            archive_threshold_days=41,
            budget_rate_window_hours=1000.0,
            loop_plan_review_expiry_days=0,
        ).validate()
    Config(
        archive_threshold_days=42,
        budget_rate_window_hours=1000.0,
        loop_plan_review_expiry_days=0,
    ).validate()


def test_archive_threshold_days_bool_rejected() -> None:
    """Neither ``True`` nor ``False`` is a real int — both must be rejected."""
    for value in (True, False):
        with pytest.raises(
            ValueError, match=r"archive_threshold_days must be 0 \(disabled\)"
        ):
            Config(archive_threshold_days=value).validate()  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Promotable-branch hint config
# ---------------------------------------------------------------------------


def test_promotable_branch_hint_defaults() -> None:
    """Config defaults match maestro.toml values."""
    cfg = Config()
    assert cfg.promotable_branch_hint_enabled is True
    assert cfg.promotable_branch_max_shown == 5


def test_experiment_cadence_comment_uses_recorded_not_completed() -> None:
    """The experiment_cadence comment block must consistently say 'recorded tasks'.

    task_count() counts every non-empty line in task_history.jsonl regardless
    of TaskOutcome (success, partial, failure).  Using 'completed'
    would imply only successful tasks are counted, which is wrong.
    """
    toml_text = (Path(__file__).resolve().parent.parent / "maestro.toml").read_text(
        encoding="utf-8"
    )
    lines = toml_text.splitlines()
    # Find the experiment_cadence key line and scan the preceding comment
    # block (up to 20 lines back) for the incorrect wording "completed tasks".
    # Comment lines use "Experiment cadence" (capital E, space) not the token
    # "experiment_cadence", so the guard must anchor on the key line and look
    # back rather than matching the comment directly.
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("experiment_cadence"):
            found = True
            start = max(0, i - 20)
            for j in range(start, i + 1):
                if "completed tasks" in lines[j]:
                    raise AssertionError(
                        f"maestro.toml line {j + 1} uses 'completed tasks' in the "
                        "experiment_cadence comment block; should be 'recorded tasks' "
                        "to match task_count() which counts all outcomes: "
                        + repr(lines[j])
                    )
            break
    assert found, (
        "experiment_cadence key not found in maestro.toml — comment guard is a no-op"
    )


def test_experiment_cadence_guard_is_not_vacuous_when_key_missing() -> None:
    """Prove the guard is non-vacuous: without 'assert found' the test would
    pass silently even when the banned phrase 'completed tasks' is present in
    TOML that lacks the experiment_cadence key entirely."""
    # TOML that omits experiment_cadence but has the banned phrase nearby
    toml_without_key = (
        "[loop]\n"
        "# Experiment cadence — inject after every N completed tasks\n"
        "# (wrong wording, no guard catches it without assert found)\n"
        "interval = 60\n"
    )
    lines = toml_without_key.splitlines()
    assertion_reached = False

    for i, line in enumerate(lines):
        if line.strip().startswith("experiment_cadence"):
            start = max(0, i - 20)
            for j in range(start, i + 1):
                if "completed tasks" in lines[j]:
                    assertion_reached = True
                    raise AssertionError("should have caught banned phrase")
            break

    # Without assert found the loop exits cleanly — vacuous pass.
    assert not assertion_reached, "guard somehow ran — unexpected"

    # Demonstrate that assert found catches the missing key.
    found = any(line.strip().startswith("experiment_cadence") for line in lines)
    with pytest.raises(AssertionError, match="experiment_cadence key not found"):
        assert found, (
            "experiment_cadence key not found in maestro.toml — comment guard is a no-op"
        )


# ---------------------------------------------------------------------------
# [evolution_fuel] gap keys (proposal 155)
# ---------------------------------------------------------------------------

#: Every ``[evolution_fuel]`` key at its committed default.
_FUEL_DEFAULTS: dict[str, Any] = {
    "max_plans_per_gap": 2,
    "red_main_grace_hours": 12.0,
    "operator_github_logins": [],
}


def _fuel_kwargs(**overrides: object) -> dict[str, object]:
    return {**_FUEL_DEFAULTS, **overrides}


def test_evolution_fuel_keys_parse() -> None:
    from maestro.config_settings import _EvolutionFuelSettings

    model = _EvolutionFuelSettings(**_fuel_kwargs(
        max_plans_per_gap=3, red_main_grace_hours=0,
        operator_github_logins=["Thomas-S", "ops-bot2"],
    ))
    assert model.evolution_fuel_max_plans_per_gap == 3
    assert model.evolution_fuel_red_main_grace_hours == 0
    assert model.evolution_fuel_operator_github_logins == ["Thomas-S", "ops-bot2"]


@pytest.mark.parametrize(
    "override",
    [
        {"max_plans_per_gap": 0},
        {"red_main_grace_hours": -1.0},
        {"red_main_grace_hours": float("nan")},
        {"operator_github_logins": ["-bad"]},
        {"operator_github_logins": ["has space"]},
        {"operator_github_logins": ["Thomas", "thomas"]},
    ],
)
def test_evolution_fuel_keys_reject_invalid(override: dict[str, object]) -> None:
    from pydantic import ValidationError

    from maestro.config_settings import _EvolutionFuelSettings

    with pytest.raises(ValidationError):
        _EvolutionFuelSettings(**_fuel_kwargs(**override))


def test_evolution_fuel_keys_defaults() -> None:
    config = Config()
    assert config.evolution_fuel_max_plans_per_gap == 2
    assert config.evolution_fuel_red_main_grace_hours == 12.0
    assert config.evolution_fuel_operator_github_logins == []


def test_evolution_fuel_keys_round_trip_from_toml(mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/toml/maestro.toml"] = _BASE_TOML.replace(
        "max_plans_per_gap = 2", "max_plans_per_gap = 4", 1,
    )
    config = build_config_via_pydantic(Path("/fake/toml"))
    assert config.evolution_fuel_max_plans_per_gap == 4


def test_check_secrets_ignores_apify_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform-credential refusal went with the revenue lineage."""
    monkeypatch.setenv("APIFY_TOKEN", "apify_api_x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test000000000000000000000000000000")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "my-secret-token")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert Config.check_secrets() == []
