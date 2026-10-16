"""Component tests for configuration loading and auth checks."""

from __future__ import annotations

import pytest

from maestro import config_settings
from maestro.config import Config
from tests.component.conftest import CONFIG_DIR, build_base_config

MINIMAL_TOML = """\
[paths]
workspace = "."
memory_dir = "data"
sqlite_state_enabled = false

[github]
repo = ""
reviewer = ""
issues_page_size = 5

[work]
focus_profile = "self_evolution"
task_framing = "self_evolution"
repo_workspace = ""
github_repo = ""
gate_source_path = "maestro/"
gate_tests_path = "tests/"
gate_lint_extra_paths = ["scripts/"]

[timeouts]
git = 30
git_quick = 10
selfcheck = 900
selfcheck_max = 1350
implement = 120
pr_creation = 60
gh = 30
shutdown = 10.0

[dashboard]
port = 8420
host = "127.0.0.1"
rejection_reason_max = 2000
max_page_size = 500
log_buffer_size = 500
trust_forwarded_proto = false
trust_forwarded_for = false
public_api_rate_limit_per_minute = 60
public_api_cache_max_age_seconds = 30
security_headers_enabled = true
login_rate_limit_per_minute = 10
approval_rate_limit_per_minute = 30
account_ui_v2 = false

[loop]
interval = 60
evolve_every = 5
task_every = 3
task_queue_max = 8
dream_every = 10
audit_self_every = 10
contemplate_every = 1
curate_every = 0
curate_enabled = false
curate_min_plan_age_hours = 0.0
plan_feedback_enabled = false
curate_skip_serviced_enabled = false
duplicate_pending_plan_threshold = 0.5
pending_plan_cluster_threshold = 2
pending_cluster_threshold = 0.35
pending_cluster_max = 2
pending_cluster_sweep_enabled = true
restatement_sweep_enabled = true
obsolete_directive_actuator_enabled = false
restatement_concept_key_enabled = false
pending_plan_submit_block_threshold = 5  # default; now honoured by from_env (proposal 106)
plan_review_expiry_days = 21
max_replans = 1
max_consecutive_remediation = 4
checkpoint_max_age_days = 7
post_implement_review = true
self_review_max_rounds = 2
self_review_own_session_enabled = false
auto_fix_max_rounds = 3
gate_convergence_ceiling = 20
gate_observation_enabled = false
conscious_workspace_observe_enabled = false
conscious_workspace_broadcast_enabled = false
gate_lease_enabled = false
gate_lease_cgroup_reserve_enabled = false
gate_lease_admission_cap_enabled = false
gate_budget_lease_owned_enabled = false
gate_scope_impacted_enabled = false
gate_environment_oracle_enabled = false
gate_lease_slot_dir = "~/.maestro/gate_slots"
host_capacity_sensor_enabled = false
runtime_contract_enforced = false
max_implementation_attempts = 3
attempt_disposition_enabled = false
max_resume_attempts = 2
max_implementations_per_iteration = 1
executor_loop_detect_repeats = 3
executor_empty_response_retries = 1
max_loop_budget_usd_per_hour = 0.0
budget_rate_window_hours = 1.0
budget_rate_min_cost_usd = 0.10
budget_throttle_max_multiplier = 60.0
archive_threshold_days = 0
standby_enabled = false
standby_start_hour_utc = 13
standby_end_hour_utc = 19
peak_throttle_enabled = false
peak_throttle_timezone = "America/Los_Angeles"
peak_throttle_start_hour = 5
peak_throttle_end_hour = 11
peak_throttle_weekdays_only = true
peak_throttle_rate_multiplier = 0.5
task_branch_reuse_enabled = true
planning_excludes_remediation_enabled = false
self_directed_tasks_enabled = true
generation_admission_enabled = false
generation_admission_theme_fraction = 0.75
generation_admission_window = 20
pre_mint_generation_gate_enabled = false
self_model_external_anchor_enabled = false
tend_prs_enabled = true
tend_prs_every = 5
tend_bug_comments_enabled = true
tend_planless_prs_enabled = true
tend_main_merge_enabled = true
resolve_addressed_threads_enabled = true
gate_strip_redirections_enabled = false
user_request_blocks_standdown_enabled = true
user_pr_message_remediation_enabled = true
pr_ready_for_review_notify_enabled = true
conflict_resolution_cooldown_hours = 24.0
quality_fix_cooldown_hours = 24.0
cooldown_backoff_base_hours = 1.0
cooldown_signature_keying_enabled = false
owed_act_binding_enabled = false
version_archive_enabled = true
version_archive_window_iterations = 10
startup_rate_limit_cooldown_seconds = 60
long_context_implementation_enabled = false
experiment_cadence = 5
promotable_backlog_threshold = 3
promotable_backlog_directive_enabled = true
promotable_branch_hint_enabled = true
promotable_branch_max_shown = 5
prior_task_context_enabled = true
integration_cadence = 5
investigation_streak_threshold = 3
investigation_streak_enabled = true
investigation_rate_window = 10
investigation_rate_threshold = 0.4
investigation_rate_enabled = true
branchless_streak_threshold = 3
readmission_construction_directive_enabled = false
readmission_construction_ratio_threshold = 0.5
remediation_noop_threshold = 2
remediation_noop_gate_enabled = true
branch_resolved_gate_enabled = true
branch_resolved_repetition_threshold = 2
merge_block_local_probe_enabled = true
pr_remediation_max_attempts_per_head = 3
pr_remediation_lap_budget = 6
merge_truth_ttl_seconds = 300
prestamp_merge_truth_gate_enabled = false
observed_ready_standdown_enabled = false
ci_pending_hold_dequeue_enabled = true
ci_pending_green_handoff_enabled = false
# These rollout flags are pinned off as a uniform synthetic baseline; this
# fixture is a minimal parseable config for the loader tests, NOT a mirror of
# maestro.toml. salvage_before_disengage_enabled and wip_primacy_enabled both
# ship true in production and deliberately diverge here — none of their values
# is asserted, so aligning one and not the others would be arbitrary churn.
salvage_before_disengage_enabled = false
mergeable_merge_request_enabled = false
frame_discharge_bind_enabled = false
wip_primacy_enabled = false
refutable_standdown_enabled = false
standdown_refutation_release_cap = 2
owned_act_preempt_enabled = false
multi_pr_proposals_enabled = false
unwinnable_race_re_entry_threshold = 3
require_claude_md_update = false
session_landing_enabled = false
obligation_fork_floor_enabled = false
owned_committed_push_actuator_enabled = false
progress_aware_lap_budget_enabled = false
dropped_directive_replay_enabled = false
max_open_evolution_prs = 5
uncalled_symbol_gate_enabled = true
uncalled_symbol_max_allowed = 0
criteria_alignment_enabled = true
quiet_skip_enabled = true
compound_scope_gate_enabled = true
investigation_observable_gate_enabled = true
plan_grounding_enabled = false
generator_reality_grounding_enabled = false
phantom_premise_gate_enabled = false
grounded_reflection_enabled = false
grounded_planning_enabled = false
grounded_planning_callers = ["propose_plan"]
grounded_planning_rejection_tool_enabled = false
stale_repeat_bet_guard_enabled = false

[sdk]
warning_throttle_base_seconds = 10
warning_throttle_hysteresis = 3
warning_hold_threshold = 3
warning_hold_probe_interval = 60
warning_hold_min_utilization = 0.95
max_concurrent_queries = 2
single_use_evolution_calls_enabled = false
call_policy_resolution_enabled = false
enforced_tool_surface_enabled = false
task_budget_tokens = 0
provider = "anthropic"
enable_session_reuse = true
stream_idle_timeout_seconds = 180
tool_stream_idle_timeout_seconds = 1800
rate_limit_idle_probe_interval = 300
rate_limit_idle_max_probe_interval = 3600

[cli]
journal_default_count = 10
log_tail_lines = 50
verbose = false

[telegram]
max_length = 4096

[messaging]
max_length = 2000

[email]
smtp_host = "smtp.zoho.com"
smtp_port = 587
imap_host = "imap.zoho.com"
imap_port = 993
max_length = 50000

[logs]
dates_shown = 5
session_data_days = 10
max_bytes = 52428800
backup_count = 3
structured_transcripts_enabled = false
transcript_content_max = 20000

[ci]
cov_fail_under = 97

[models]
default = "claude-opus-4-6"
fallback = ""
long_context = ""

[mcp]
context7_enabled = false
context7_command = "npx"
context7_args = ["-y", "@upstash/context7-mcp"]
serena_enabled = false
serena_command = "uvx"
serena_args = ["--from", "serena-agent==1.5.3", "serena", "start-mcp-server"]

[review]
max_diff_chars = 80000
max_files = 50
gh_timeout = 30

[artifacts]
review_enabled = false
review_every = 20
review_batch_size = 3
review_max_file_bytes = 4096
review_max_files_per_task = 10
review_max_bundle_bytes = 12288

[publication]
enabled = false
owner = ""
copyright_holder = "Thomas Sodon"
license = "MIT"
secret_scan_enabled = true
executing_stale_timeout_seconds = 600
push_retry_limit = 3

[evolution_fuel]
max_plans_per_gap = 2
red_main_grace_hours = 12.0
operator_github_logins = []

[audit]
stall_threshold = 5
stall_block_enabled = false
stale_entry_enabled = false
impact_review_enabled = true
impact_review_after_days = 7
impact_review_window = 10
settlement_reconciler_enabled = false
settlement_record_reconciler_enabled = false
orphaned_cure_enabled = true
orphaned_cure_age_cycles = 3
orphaned_cure_retire_enabled = true
orphaned_cure_retire_age_cycles = 30
phantom_escalation_enabled = true
phantom_escalation_delivery_enabled = false
unconfirmed_landing_enabled = true
unconfirmed_landing_age_cycles = 3
unconfirmed_landing_forced_enabled = false
ungrounded_closure_enabled = false
construction_dominance_enabled = false
construction_dominance_threshold = 0.6
construction_dominance_window = 10
phantom_partial_prune_enabled = false
undercredited_landing_enabled = false
reconcile_near_miss_enabled = false
reconcile_near_max_behind = 10
green_ready_handoff_enabled = true
banked_owned_act_enabled = false
owned_act_drought_enabled = true
owned_act_drought_window = 5
owned_act_drought_min_backlog = 2
branch_promotion_pressure_enabled = true
reality_refutation_display_enabled = false
reality_refutation_threshold = 3
reality_refutation_enabled = false
costume_display_enabled = false
undefined_coinage_display_enabled = false
flag_horizon_display_enabled = false
split_due_sensor_enabled = false
split_due_directive_enabled = false
undefined_coinage_directive_enabled = false
retire_unlanded_limitations_enabled = false
generative_debt_threshold = 3
self_calibration_enabled = false
calibration_verdict_authority_enabled = false
evidence_bound_claims_enabled = false
closed_reference_enabled = false
refutation_accountability_enabled = false
refutation_rejection_cycles = 3
"""


def test_config_from_env_with_real_toml(tmp_path, monkeypatch):
    toml_path = tmp_path / "maestro.toml"
    toml_path.write_text(MINIMAL_TOML)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("MAESTRO_ENV", raising=False)
    config = Config.from_env(env_file=None, config_dir=tmp_path)
    assert config.git_timeout == 30
    assert config.dashboard_port == 8420
    assert config.loop_interval == 60
    assert config.telegram_max_length == 4096
    assert config.log_dates_shown == 5
    assert config.cov_fail_under == 97
    assert config.task_branch_reuse_enabled is True
    assert config.self_directed_tasks_enabled is True


def test_config_env_overlay(tmp_path, monkeypatch):
    base = tmp_path / "maestro.toml"
    base.write_text(MINIMAL_TOML)
    overlay = tmp_path / "maestro.staging.toml"
    overlay.write_text("""\
[dashboard]
port = 9999

[loop]
interval = 120
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("MAESTRO_ENV", "staging")
    config = Config.from_env(env_file=None, config_dir=tmp_path)
    assert config.dashboard_port == 9999
    assert config.loop_interval == 120
    # Non-overlaid values remain at base
    assert config.git_timeout == 30


def test_config_from_env_carries_base_sdk_provider(tmp_path, monkeypatch):
    """Base ``[sdk] provider`` round-trips through ``from_env``."""
    toml_path = tmp_path / "maestro.toml"
    toml_path.write_text(MINIMAL_TOML)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("MAESTRO_ENV", raising=False)
    config = Config.from_env(env_file=None, config_dir=tmp_path)
    assert config.sdk_provider == "anthropic"
    assert config.sdk_phase_providers == {}
    assert config.resolved_sdk_provider() == "anthropic"


def test_config_from_env_wires_overridden_sdk_provider(tmp_path, monkeypatch):
    """Operator-set ``provider`` / per-phase overrides reach the Config (#058)."""
    base = tmp_path / "maestro.toml"
    base.write_text(MINIMAL_TOML)
    overlay = tmp_path / "maestro.staging.toml"
    overlay.write_text("""\
[sdk]
provider = "openai"
artifact_review_provider = "cli_paced"
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("MAESTRO_ENV", "staging")
    config = Config.from_env(env_file=None, config_dir=tmp_path)
    assert config.sdk_provider == "openai"
    assert config.resolved_sdk_provider() == "openai"
    assert config.sdk_phase_providers == {"artifact_review": "cli_paced"}
    assert config.provider_for_phase("artifact_review") == "cli_paced"
    assert config.provider_for_phase("contemplation") == "openai"


def test_config_from_env_wires_cli_paced_overrides(tmp_path, monkeypatch):
    """``[sdk.cli_paced]`` Cadence + idle overrides reach the Config (#056 Step 12)."""
    base = tmp_path / "maestro.toml"
    base.write_text(MINIMAL_TOML)
    overlay = tmp_path / "maestro.staging.toml"
    overlay.write_text("""\
[sdk.cli_paced]
mean_key_ms = 70
heartbeat_per_answer_char_ms = 8
stream_idle_timeout_seconds = 150
probe_metrics_enabled = true
probe_metrics_every = 3
""")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("MAESTRO_ENV", "staging")
    config = Config.from_env(env_file=None, config_dir=tmp_path)
    assert config.sdk_cli_paced_cadence == {
        "mean_key_ms": 70.0,
        "heartbeat_per_answer_char_ms": 8.0,
    }
    assert config.sdk_cli_paced_stream_idle_timeout_seconds == 150.0
    # Slash-probe knobs (proposal 062 Step 6) ride the same table.
    assert config.sdk_cli_paced_probe_metrics_enabled is True
    assert config.sdk_cli_paced_probe_metrics_every == 3


def test_config_secrets_from_env(tmp_path, monkeypatch):
    """Secrets are still loaded from environment variables."""
    toml_path = tmp_path / "maestro.toml"
    toml_path.write_text(MINIMAL_TOML)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "my-token")
    monkeypatch.delenv("MAESTRO_ENV", raising=False)
    config = Config.from_env(env_file=None, config_dir=tmp_path)
    assert config.telegram_bot_token == "123456:ABC"
    assert config.dashboard_token == "my-token"


def test_config_auth_check_no_keys(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        Config.check_auth()
    assert "No Claude authentication found" in str(exc_info.value)


def test_config_auth_check_oauth_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    result = Config.check_auth()
    assert result == "oauth"


# Flags ``maestro.production.toml`` flips away from their base ``maestro.toml``
# value.  Naming them anchors the guard below against a vacuous pass without
# coupling it to the overlay's current size.
_PRODUCTION_OVERRIDDEN_LEVERS = (
    "gate_observation_enabled",
    "grounded_reflection_enabled",
    "attempt_disposition_enabled",
    "host_capacity_sensor_enabled",
)

# Fields the component ``config`` fixture deliberately replaces after loading,
# so they are expected to differ from a freshly-loaded base config.
_FIXTURE_OVERLAID_FIELDS = frozenset({
    "memory_dir",
    "workspace",
    "github_repo",
    "telegram_bot_token",
    "telegram_chat_id",
    "enable_session_reuse",
})


@pytest.fixture
def _production_env(monkeypatch):
    """Set a hostile ambient ``MAESTRO_ENV`` before any config is built.

    Declared ahead of ``config`` in a test signature, pytest instantiates it
    first, so the ``config`` fixture body runs with the overlay env present.
    """
    monkeypatch.setenv("MAESTRO_ENV", "production")


def test_component_config_fixture_ignores_ambient_env(_production_env, config):
    """The component ``config`` fixture never inherits the MAESTRO_ENV overlay.

    Guards #3460: the fixture builds its ``Config`` through
    :func:`build_base_config`, which pins the env name to ``None``, so a
    developer (or the production container) running the suite with
    ``MAESTRO_ENV=production`` gets byte-identical component behavior to CI.
    This exercises the *real* fixture under the hostile env, so reverting the
    fixture body to ``Config.from_env`` fails here.  Every flag
    ``maestro.production.toml`` flips must still read its base value.
    """
    # What the ambient env *would* have produced had the overlay been consulted.
    with_overlay = config_settings.build_config_authoritative(
        CONFIG_DIR, "production",
    )
    # The env-unset reference the CI suite runs against — ``build_base_config``
    # pins ``env_name=None``, so the ambient ``MAESTRO_ENV`` cannot reach it.
    reference = build_base_config()

    overridden = [
        name
        for name in sorted(type(reference).__dataclass_fields__)
        if getattr(reference, name) != getattr(with_overlay, name)
    ]
    # The overlay genuinely differs on the levers this guard cares about —
    # otherwise the comparison below would pass vacuously.
    for lever in _PRODUCTION_OVERRIDDEN_LEVERS:
        assert lever in overridden, lever

    for name in overridden:
        if name in _FIXTURE_OVERLAID_FIELDS:
            continue
        assert getattr(config, name) == getattr(reference, name), name

    # The behavior-changing levers the original leak turned on/off.
    assert config.gate_observation_enabled is False
    assert config.grounded_reflection_enabled is False
    assert config.attempt_disposition_enabled is False
    assert config.host_capacity_sensor_enabled is False
