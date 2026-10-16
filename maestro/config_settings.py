"""Declarative ``pydantic-settings`` parser for :class:`maestro.config.Config`.

Proposal 106. This module is the **sole** ``maestro.toml`` parser: nested
``pydantic-settings`` models mirror the TOML sections, with ``Field(...)``
constraints standing in for what was once a hand-maintained
``(section, key) -> (field, type)`` mapping table plus a ``validate()``
raise-ladder. ``Config.from_env`` builds every Config through
:func:`build_config_authoritative`, a thin ``ValidationError`` -> ``ValueError``
wrapper over :func:`build_config_via_pydantic` — there is no longer a legacy
loader to cross-check against.

An unknown key inside a modelled section (via ``extra="forbid"`` on
:class:`_Section`) and an unknown top-level section (via the guard in
:func:`build_config_via_pydantic`) each fail loud at load, replacing the former
loader's silent drop-and-warn; so does a typo'd key or mistyped value in the
non-flat ``[sdk]`` sub-structures (``<phase>_provider``, ``[sdk.pricing.*]``,
``[sdk.cli_paced]``), which the dedicated sub-table parsers reject. Cross-field and ``[sdk]`` sub-table validation
that the ``Field`` constraints cannot express lives in
:meth:`maestro.config.Config._check_cross_field_and_subtable`, which
:func:`build_config_via_pydantic` runs on the assembled ``Config`` so the
declarative build enforces the complete rejection surface itself. ``Config.validate``
is not on the load path: proposal 106 retired its flat-scalar raise-ladder (the
section models reproduce it), leaving a one-line delegator to
:meth:`~maestro.config.Config._check_cross_field_and_subtable` — the direct
dataclass-construction entry point for the cross-field/sub-table guards.

Design notes:

* The base + ``{env}`` overlay are merged with
  :func:`maestro.toml_config._deep_merge` (reused, not re-derived), and each file
  is read with :func:`maestro.toml_config._read_toml`; ``toml_config`` is now a
  focused "read + overlay merge" module the declarative parse calls into.
* Every modelled flat scalar/list field is *required* on its section model: a
  valid ``maestro.toml`` supplies every key, so there is no absent-flat-key case
  and no dataclass-default to mirror — eliminating a whole class of bugs.
* The three non-flat ``[sdk]`` sub-structures — ``sdk_phase_providers``,
  ``sdk_pricing``, and the ``[sdk.cli_paced]`` knobs — are not expressible as
  flat keyed scalars, so they are parsed by dedicated helpers and spread onto the
  ``Config`` only when present (the "absent nested table -> dataclass default"
  posture).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from maestro.config import (
    INT_CLI_PACED_CADENCE_KEYS,
    VALID_CLI_PACED_CADENCE_KEYS,
    VALID_SDK_PROVIDERS,
    VALID_WORK_FRAMINGS,
    Config,
)
from maestro.toml_config import _KNOWN_EXTERNAL_SECTIONS, _deep_merge, _read_toml

#: The ``MAESTRO_ENV`` validity rule applied before reading a ``{env}`` overlay:
#: alphanumerics, hyphens, and underscores only (blocks a path-traversal env name).
_ENV_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _reject_bool(value: Any) -> Any:
    """Reject a Python ``bool`` supplied for a numeric field, else pass it through.

    pydantic treats ``bool`` as a subclass of ``int``, so a boolean handed to an
    ``int``/``float`` field is silently coerced to ``1``/``0`` rather than
    rejected — meaning a mistyped ``maestro.toml`` line (``interval = true``)
    would load as ``1`` instead of failing loud at startup. Running as a
    ``BeforeValidator`` (ahead of pydantic's numeric coercion) this raises on a
    boolean while leaving every genuine ``int``/``float`` — including a TOML int
    supplied for a float field — untouched for normal coercion.
    """
    if isinstance(value, bool):
        raise ValueError("must be a number, not a boolean")
    return value


#: ``int``/``float`` field types that reject a boolean before coercion. Every
#: numeric section-model field is annotated with one of these instead of the bare
#: ``int``/``float`` so the authoritative load path fails loud on a boolean rather
#: than silently coercing it, matching what the retired ``Config.validate``
#: raise-ladder rejected.
_NumericInt = Annotated[int, BeforeValidator(_reject_bool)]
_NumericFloat = Annotated[float, BeforeValidator(_reject_bool)]


class _Section(BaseSettings):
    """Base for every TOML-section model.

    Each section model is populated from an explicit ``dict`` (the parsed
    ``maestro.toml`` section) — never from the process environment. Overriding
    ``settings_customise_sources`` to return only the init source makes that
    isolation exact: an ``os.environ`` var that happens to share a field name can
    never leak in, keeping the declarative parse deterministic.
    ``populate_by_name`` lets callers construct by either the TOML key (alias) or
    the ``Config`` field name; ``extra="forbid"`` turns a key present in a
    modelled section that no field maps into a load-time error, so a stray/typo'd
    key fails loud at load rather than being silently dropped. The three
    hand-parsed non-flat ``[sdk]`` sub-structures the model deliberately does not
    declare are stripped by :func:`_strip_sdk_nonflat_keys` before ``_SdkSettings``
    is constructed, so ``extra="forbid"`` rejects only genuinely-unknown
    ``[sdk]`` keys.
    """

    model_config = SettingsConfigDict(extra="forbid", populate_by_name=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


class _PathsSettings(_Section):
    workspace: str
    memory_dir: str
    sqlite_state_enabled: bool


class _GithubSettings(_Section):
    # ``repo`` / ``reviewer`` may be empty strings (coerced to ``None`` by the
    # builder), so they carry no ``min_length``.
    github_repo: str = Field(alias="repo")
    github_reviewer: str = Field(alias="reviewer")
    github_issues_page_size: _NumericInt = Field(alias="issues_page_size", ge=1)


class _WorkSettings(_Section):
    work_focus_profile: str = Field(alias="focus_profile")
    work_task_framing: str = Field(alias="task_framing")
    work_repo_workspace: str = Field(alias="repo_workspace")
    work_github_repo: str = Field(alias="github_repo")
    work_gate_source_path: str = Field(alias="gate_source_path")
    work_gate_tests_path: str = Field(alias="gate_tests_path")
    work_gate_lint_extra_paths: list[str] = Field(alias="gate_lint_extra_paths")

    @field_validator("work_focus_profile", "work_task_framing")
    @classmethod
    def _check_framing(cls, value: str) -> str:
        if value not in VALID_WORK_FRAMINGS:
            raise ValueError(
                f"must be one of {sorted(VALID_WORK_FRAMINGS)}, got {value!r}"
            )
        return value


class _TimeoutsSettings(_Section):
    git_timeout: _NumericInt = Field(alias="git", gt=0)
    git_quick_timeout: _NumericInt = Field(alias="git_quick", gt=0)
    selfcheck_timeout: _NumericInt = Field(alias="selfcheck", gt=0)
    selfcheck_max_timeout: _NumericInt = Field(alias="selfcheck_max", gt=0)
    implement_timeout: _NumericInt = Field(alias="implement", gt=0)
    pr_creation_timeout: _NumericInt = Field(alias="pr_creation", gt=0)
    gh_timeout: _NumericInt = Field(alias="gh", gt=0)
    shutdown_timeout: _NumericFloat = Field(alias="shutdown", gt=0, allow_inf_nan=False)


class _DashboardSettings(_Section):
    dashboard_port: _NumericInt = Field(alias="port", ge=1, le=65535)
    dashboard_host: str = Field(alias="host", min_length=1)
    rejection_reason_max: _NumericInt = Field(ge=1)
    max_page_size: _NumericInt = Field(ge=1)
    log_buffer_size: _NumericInt = Field(ge=1)
    trust_forwarded_proto: bool
    trust_forwarded_for: bool
    public_api_rate_limit_per_minute: _NumericInt = Field(ge=1)
    public_api_cache_max_age_seconds: _NumericInt = Field(ge=0, le=3600)
    security_headers_enabled: bool
    login_rate_limit_per_minute: _NumericInt = Field(ge=1)
    approval_rate_limit_per_minute: _NumericInt = Field(ge=1)
    account_ui_v2: bool


class _LoopSettings(_Section):
    loop_interval: _NumericInt = Field(alias="interval", ge=1)
    loop_evolve_every: _NumericInt = Field(alias="evolve_every", ge=0)
    loop_task_every: _NumericInt = Field(alias="task_every", ge=0)
    task_queue_max: _NumericInt = Field(ge=0, le=1000)
    loop_dream_every: _NumericInt = Field(alias="dream_every", ge=0)
    loop_audit_self_every: _NumericInt = Field(alias="audit_self_every", ge=0)
    loop_contemplate_every: _NumericInt = Field(alias="contemplate_every", ge=0)
    loop_curate_every: _NumericInt = Field(alias="curate_every", ge=0)
    loop_curate_enabled: bool = Field(alias="curate_enabled")
    loop_curate_min_plan_age_hours: _NumericFloat = Field(
        alias="curate_min_plan_age_hours", ge=0, allow_inf_nan=False
    )
    loop_plan_feedback_enabled: bool = Field(alias="plan_feedback_enabled")
    loop_curate_skip_serviced_enabled: bool = Field(
        alias="curate_skip_serviced_enabled"
    )
    # The pending-plan cluster / restatement-sweep knobs carry no ``validate()``
    # range, so they take no ``ge``/``gt``; the two floats keep the finite check
    # via ``allow_inf_nan=False`` like every other modelled float.
    loop_duplicate_pending_plan_threshold: _NumericFloat = Field(
        alias="duplicate_pending_plan_threshold", allow_inf_nan=False
    )
    loop_pending_plan_cluster_threshold: _NumericInt = Field(
        alias="pending_plan_cluster_threshold"
    )
    loop_pending_cluster_threshold: _NumericFloat = Field(
        alias="pending_cluster_threshold", allow_inf_nan=False
    )
    loop_pending_cluster_max: _NumericInt = Field(alias="pending_cluster_max")
    loop_pending_cluster_sweep_enabled: bool = Field(
        alias="pending_cluster_sweep_enabled"
    )
    loop_restatement_sweep_enabled: bool = Field(alias="restatement_sweep_enabled")
    loop_obsolete_directive_actuator_enabled: bool = Field(
        alias="obsolete_directive_actuator_enabled"
    )
    loop_restatement_concept_key_enabled: bool = Field(
        alias="restatement_concept_key_enabled"
    )
    loop_pending_plan_submit_block_threshold: _NumericInt = Field(
        alias="pending_plan_submit_block_threshold"
    )
    loop_plan_review_expiry_days: _NumericInt = Field(alias="plan_review_expiry_days")
    loop_max_replans: _NumericInt = Field(alias="max_replans", ge=0)
    max_consecutive_remediation: _NumericInt = Field(ge=0)
    checkpoint_max_age_days: _NumericInt = Field(ge=0)
    post_implement_review: bool
    task_branch_reuse_enabled: bool
    planning_excludes_remediation_enabled: bool
    self_directed_tasks_enabled: bool
    generation_admission_enabled: bool
    generation_admission_theme_fraction: _NumericFloat = Field(
        ge=0.0, le=1.0, allow_inf_nan=False
    )
    generation_admission_window: _NumericInt = Field(ge=1)
    pre_mint_generation_gate_enabled: bool
    self_model_external_anchor_enabled: bool
    tend_prs_enabled: bool
    tend_prs_every: _NumericInt = Field(ge=1)
    tend_bug_comments_enabled: bool
    tend_planless_prs_enabled: bool
    tend_main_merge_enabled: bool
    resolve_addressed_threads_enabled: bool
    gate_strip_redirections_enabled: bool
    user_request_blocks_standdown_enabled: bool
    user_pr_message_remediation_enabled: bool
    pr_ready_for_review_notify_enabled: bool
    conflict_resolution_cooldown_hours: _NumericFloat = Field(gt=0, allow_inf_nan=False)
    quality_fix_cooldown_hours: _NumericFloat = Field(gt=0, allow_inf_nan=False)
    cooldown_backoff_base_hours: _NumericFloat = Field(gt=0, allow_inf_nan=False)
    cooldown_signature_keying_enabled: bool
    owed_act_binding_enabled: bool
    version_archive_enabled: bool
    version_archive_window_iterations: _NumericInt = Field(ge=1)
    max_loop_budget_usd_per_hour: _NumericFloat = Field(ge=0, allow_inf_nan=False)
    budget_rate_window_hours: _NumericFloat = Field(gt=0, allow_inf_nan=False)
    budget_rate_min_cost_usd: _NumericFloat = Field(ge=0, allow_inf_nan=False)
    budget_throttle_max_multiplier: _NumericFloat = Field(ge=1.0, allow_inf_nan=False)
    archive_threshold_days: _NumericInt = Field(ge=0)
    startup_rate_limit_cooldown_seconds: _NumericInt = Field(ge=0)
    standby_enabled: bool
    standby_start_hour_utc: _NumericInt = Field(ge=0, le=23)
    standby_end_hour_utc: _NumericInt = Field(ge=0, le=23)
    peak_throttle_enabled: bool
    peak_throttle_timezone: str = Field(min_length=1)
    peak_throttle_start_hour: _NumericInt = Field(ge=0, le=23)
    peak_throttle_end_hour: _NumericInt = Field(ge=0, le=23)
    peak_throttle_weekdays_only: bool
    peak_throttle_rate_multiplier: _NumericFloat = Field(gt=0, allow_inf_nan=False)
    long_context_implementation_enabled: bool
    experiment_cadence: _NumericInt = Field(ge=1)
    promotable_backlog_threshold: _NumericInt = Field(ge=0)
    promotable_backlog_directive_enabled: bool
    promotable_branch_hint_enabled: bool
    promotable_branch_max_shown: _NumericInt = Field(ge=1)
    prior_task_context_enabled: bool
    integration_cadence: _NumericInt = Field(ge=1)
    branchless_streak_threshold: _NumericInt = Field(ge=1)
    investigation_streak_threshold: _NumericInt = Field(ge=1)
    investigation_streak_enabled: bool
    investigation_rate_window: _NumericInt = Field(ge=1)
    investigation_rate_threshold: _NumericFloat = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    investigation_rate_enabled: bool
    readmission_construction_directive_enabled: bool
    readmission_construction_ratio_threshold: _NumericFloat = Field(
        ge=0.0, le=1.0, allow_inf_nan=False
    )
    remediation_noop_threshold: _NumericInt = Field(ge=1)
    remediation_noop_gate_enabled: bool
    branch_resolved_gate_enabled: bool
    branch_resolved_repetition_threshold: _NumericInt = Field(ge=1)
    merge_block_local_probe_enabled: bool
    pr_remediation_max_attempts_per_head: _NumericInt = Field(ge=1)
    pr_remediation_lap_budget: _NumericInt = Field(ge=0)
    merge_truth_ttl_seconds: _NumericInt = Field(ge=0)
    prestamp_merge_truth_gate_enabled: bool
    observed_ready_standdown_enabled: bool
    ci_pending_hold_dequeue_enabled: bool
    ci_pending_green_handoff_enabled: bool
    salvage_before_disengage_enabled: bool
    mergeable_merge_request_enabled: bool
    frame_discharge_bind_enabled: bool
    wip_primacy_enabled: bool
    refutable_standdown_enabled: bool
    standdown_refutation_release_cap: _NumericInt = Field(ge=1)
    owned_act_preempt_enabled: bool
    multi_pr_proposals_enabled: bool
    unwinnable_race_re_entry_threshold: _NumericInt = Field(ge=1)
    require_claude_md_update: bool
    session_landing_enabled: bool
    obligation_fork_floor_enabled: bool
    owned_committed_push_actuator_enabled: bool
    progress_aware_lap_budget_enabled: bool
    dropped_directive_replay_enabled: bool
    uncalled_symbol_gate_enabled: bool
    criteria_alignment_enabled: bool
    compound_scope_gate_enabled: bool
    phantom_premise_gate_enabled: bool
    investigation_observable_gate_enabled: bool
    plan_grounding_enabled: bool
    generator_reality_grounding_enabled: bool
    uncalled_symbol_max_allowed: _NumericInt = Field(ge=0)
    quiet_skip_enabled: bool
    self_review_max_rounds: _NumericInt = Field(ge=0)
    self_review_own_session_enabled: bool
    auto_fix_max_rounds: _NumericInt = Field(ge=0)
    gate_convergence_ceiling: _NumericInt = Field(ge=1)
    gate_observation_enabled: bool
    conscious_workspace_observe_enabled: bool
    conscious_workspace_broadcast_enabled: bool
    gate_lease_enabled: bool
    gate_lease_cgroup_reserve_enabled: bool
    gate_lease_admission_cap_enabled: bool
    gate_budget_lease_owned_enabled: bool
    gate_scope_impacted_enabled: bool
    gate_environment_oracle_enabled: bool
    gate_lease_slot_dir: str
    host_capacity_sensor_enabled: bool
    runtime_contract_enforced: bool
    max_implementation_attempts: _NumericInt = Field(ge=1)
    attempt_disposition_enabled: bool
    max_resume_attempts: _NumericInt = Field(ge=1)
    grounded_reflection_enabled: bool
    grounded_planning_enabled: bool
    grounded_planning_callers: list[str]
    grounded_planning_rejection_tool_enabled: bool
    stale_repeat_bet_guard_enabled: bool
    max_implementations_per_iteration: _NumericInt = Field(ge=1)
    max_open_evolution_prs: _NumericInt
    executor_loop_detect_repeats: _NumericInt = Field(ge=2)
    executor_empty_response_retries: _NumericInt = Field(ge=0)


class _AuditSettings(_Section):
    audit_stall_threshold: _NumericInt = Field(alias="stall_threshold", ge=1)
    audit_stall_block_enabled: bool = Field(alias="stall_block_enabled")
    audit_stale_entry_enabled: bool = Field(alias="stale_entry_enabled")
    audit_impact_review_enabled: bool = Field(alias="impact_review_enabled")
    audit_impact_review_after_days: _NumericInt = Field(
        alias="impact_review_after_days", ge=0
    )
    audit_impact_review_window: _NumericInt = Field(alias="impact_review_window", ge=1)
    settlement_reconciler_enabled: bool
    settlement_record_reconciler_enabled: bool
    orphaned_cure_enabled: bool
    orphaned_cure_age_cycles: _NumericInt = Field(ge=1)
    orphaned_cure_retire_enabled: bool
    orphaned_cure_retire_age_cycles: _NumericInt = Field(ge=1)
    phantom_escalation_enabled: bool
    phantom_escalation_delivery_enabled: bool
    unconfirmed_landing_enabled: bool
    unconfirmed_landing_age_cycles: _NumericInt = Field(ge=1)
    unconfirmed_landing_forced_enabled: bool
    audit_ungrounded_closure_enabled: bool = Field(alias="ungrounded_closure_enabled")
    construction_dominance_enabled: bool
    construction_dominance_threshold: _NumericFloat = Field(
        ge=0.0, le=1.0, allow_inf_nan=False
    )
    construction_dominance_window: _NumericInt = Field(ge=1)
    phantom_partial_prune_enabled: bool
    undercredited_landing_enabled: bool
    reconcile_near_miss_enabled: bool
    reconcile_near_max_behind: _NumericInt = Field(ge=1)
    green_ready_handoff_enabled: bool
    banked_owned_act_enabled: bool
    owned_act_drought_enabled: bool
    branch_promotion_pressure_enabled: bool
    owned_act_drought_window: _NumericInt = Field(ge=1)
    owned_act_drought_min_backlog: _NumericInt = Field(ge=1)
    audit_reality_refutation_display_enabled: bool = Field(
        alias="reality_refutation_display_enabled"
    )
    audit_undefined_coinage_display_enabled: bool = Field(
        alias="undefined_coinage_display_enabled"
    )
    audit_costume_display_enabled: bool = Field(alias="costume_display_enabled")
    audit_flag_horizon_display_enabled: bool = Field(
        alias="flag_horizon_display_enabled"
    )
    audit_split_due_sensor_enabled: bool = Field(alias="split_due_sensor_enabled")
    audit_split_due_directive_enabled: bool = Field(alias="split_due_directive_enabled")
    audit_undefined_coinage_directive_enabled: bool = Field(
        alias="undefined_coinage_directive_enabled"
    )
    audit_reality_refutation_threshold: _NumericInt = Field(
        alias="reality_refutation_threshold", ge=1
    )
    audit_reality_refutation_enabled: bool = Field(alias="reality_refutation_enabled")
    retire_unlanded_limitations_enabled: bool
    generative_debt_threshold: _NumericInt = Field(ge=1)
    audit_self_calibration_enabled: bool = Field(alias="self_calibration_enabled")
    audit_calibration_verdict_authority_enabled: bool = Field(
        alias="calibration_verdict_authority_enabled"
    )
    audit_evidence_bound_claims_enabled: bool = Field(
        alias="evidence_bound_claims_enabled"
    )
    audit_closed_reference_enabled: bool = Field(
        alias="closed_reference_enabled"
    )
    audit_refutation_accountability_enabled: bool = Field(
        alias="refutation_accountability_enabled"
    )
    audit_refutation_rejection_cycles: _NumericInt = Field(
        alias="refutation_rejection_cycles", ge=1
    )


class _CliSettings(_Section):
    journal_default_count: _NumericInt = Field(ge=1)
    log_tail_lines: _NumericInt = Field(ge=1)
    verbose: bool


class _TelegramSettings(_Section):
    telegram_max_length: _NumericInt = Field(alias="max_length", ge=1)


class _LogsSettings(_Section):
    log_dates_shown: _NumericInt = Field(alias="dates_shown", ge=1)
    session_data_days: _NumericInt = Field(ge=1)
    log_max_bytes: _NumericInt = Field(alias="max_bytes", ge=1)
    log_backup_count: _NumericInt = Field(alias="backup_count", ge=1)
    structured_transcripts_enabled: bool
    transcript_content_max: _NumericInt = Field(ge=1)


class _CiSettings(_Section):
    cov_fail_under: _NumericInt = Field(ge=0, le=100)


class _ModelsSettings(_Section):
    sdk_model: str = Field(alias="default")
    sdk_fallback_model: str = Field(alias="fallback")
    sdk_model_long_context: str = Field(alias="long_context")


class _SdkSettings(_Section):
    sdk_warning_throttle_base_seconds: _NumericInt = Field(
        alias="warning_throttle_base_seconds", ge=1
    )
    sdk_warning_throttle_hysteresis: _NumericInt = Field(
        alias="warning_throttle_hysteresis", ge=1
    )
    sdk_warning_hold_threshold: _NumericInt = Field(alias="warning_hold_threshold", ge=1)
    sdk_warning_hold_probe_interval: _NumericInt = Field(
        alias="warning_hold_probe_interval", ge=10
    )
    sdk_warning_hold_min_utilization: _NumericFloat = Field(
        alias="warning_hold_min_utilization", ge=0, le=1, allow_inf_nan=False
    )
    sdk_rate_limit_idle_probe_interval: _NumericInt = Field(
        alias="rate_limit_idle_probe_interval", ge=10
    )
    sdk_rate_limit_idle_max_probe_interval: _NumericInt = Field(
        alias="rate_limit_idle_max_probe_interval", ge=10
    )
    sdk_max_concurrent_queries: _NumericInt = Field(alias="max_concurrent_queries", ge=1)
    sdk_single_use_evolution_calls_enabled: bool = Field(
        alias="single_use_evolution_calls_enabled"
    )
    sdk_call_policy_resolution_enabled: bool = Field(
        alias="call_policy_resolution_enabled"
    )
    sdk_enforced_tool_surface_enabled: bool = Field(
        alias="enforced_tool_surface_enabled"
    )
    sdk_stream_idle_timeout_seconds: _NumericFloat = Field(
        alias="stream_idle_timeout_seconds", ge=0, allow_inf_nan=False
    )
    sdk_tool_stream_idle_timeout_seconds: _NumericFloat = Field(
        alias="tool_stream_idle_timeout_seconds", ge=0, allow_inf_nan=False
    )
    sdk_task_budget_tokens: _NumericInt = Field(alias="task_budget_tokens", ge=0)
    sdk_provider: str = Field(alias="provider")
    enable_session_reuse: bool

    @field_validator("sdk_provider")
    @classmethod
    def _check_provider(cls, value: str) -> str:
        if value not in VALID_SDK_PROVIDERS:
            raise ValueError(
                f"sdk_provider must be one of {sorted(VALID_SDK_PROVIDERS)}, "
                f"got {value!r}"
            )
        return value


class _MessagingSettings(_Section):
    message_max_length: _NumericInt = Field(alias="max_length", ge=1)


class _EmailSettings(_Section):
    email_smtp_host: str = Field(alias="smtp_host")
    email_smtp_port: _NumericInt = Field(alias="smtp_port", ge=1, le=65535)
    email_imap_host: str = Field(alias="imap_host")
    email_imap_port: _NumericInt = Field(alias="imap_port", ge=1, le=65535)
    email_max_length: _NumericInt = Field(alias="max_length", ge=1)


class _ArtifactsSettings(_Section):
    artifact_review_enabled: bool = Field(alias="review_enabled")
    artifact_review_every: _NumericInt = Field(alias="review_every", ge=1)
    artifact_review_batch_size: _NumericInt = Field(alias="review_batch_size", ge=1)
    artifact_review_max_file_bytes: _NumericInt = Field(alias="review_max_file_bytes", ge=1)
    artifact_review_max_files_per_task: _NumericInt = Field(
        alias="review_max_files_per_task", ge=1
    )
    artifact_review_max_bundle_bytes: _NumericInt = Field(
        alias="review_max_bundle_bytes", ge=1
    )


class _PublicationSettings(_Section):
    publication_enabled: bool = Field(alias="enabled")
    publication_owner: str = Field(alias="owner")
    publication_copyright_holder: str = Field(alias="copyright_holder", min_length=1)
    publication_license: str = Field(alias="license", min_length=1)
    publication_secret_scan_enabled: bool = Field(alias="secret_scan_enabled")
    publication_executing_stale_timeout_seconds: _NumericInt = Field(
        alias="executing_stale_timeout_seconds", ge=60
    )
    publication_push_retry_limit: _NumericInt = Field(alias="push_retry_limit", ge=1)


class _McpSettings(_Section):
    context7_mcp_enabled: bool = Field(alias="context7_enabled")
    context7_mcp_command: str = Field(alias="context7_command", min_length=1)
    context7_mcp_args: list[str] = Field(alias="context7_args")
    serena_mcp_enabled: bool = Field(alias="serena_enabled")
    serena_mcp_command: str = Field(alias="serena_command", min_length=1)
    serena_mcp_args: list[str] = Field(alias="serena_args")


_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class _EvolutionFuelSettings(_Section):
    evolution_fuel_max_plans_per_gap: _NumericInt = Field(alias="max_plans_per_gap", ge=1)
    evolution_fuel_red_main_grace_hours: _NumericFloat = Field(
        alias="red_main_grace_hours", ge=0, allow_inf_nan=False,
    )
    evolution_fuel_operator_github_logins: list[str] = Field(alias="operator_github_logins")

    @field_validator("evolution_fuel_operator_github_logins")
    @classmethod
    def _check_operator_logins(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for login in value:
            if not _GITHUB_LOGIN_RE.match(login):
                raise ValueError(f"{login!r} is not a GitHub login")
            folded = login.casefold()
            if folded in seen:
                raise ValueError(f"duplicate login {login!r} (case-insensitive)")
            seen.add(folded)
        return value


#: TOML section name -> its declarative model, and the source of truth for the
#: ``(section, key) -> Config field`` structural guards. The union of every
#: model's fields mirrors the committed ``maestro.toml``'s flat scalar/list keys
#: exactly, with no exceptions (guarded by ``tests/test_config_settings_parity.py``
#: and ``tests/test_config_flag_lifecycle.py``). ``[sdk.pricing.*]`` /
#: ``[sdk.cli_paced]`` / ``<phase>_provider`` are non-flat and handled below.
_SECTION_MODELS: dict[str, type[_Section]] = {
    "paths": _PathsSettings,
    "github": _GithubSettings,
    "work": _WorkSettings,
    "timeouts": _TimeoutsSettings,
    "dashboard": _DashboardSettings,
    "loop": _LoopSettings,
    "audit": _AuditSettings,
    "cli": _CliSettings,
    "telegram": _TelegramSettings,
    "logs": _LogsSettings,
    "ci": _CiSettings,
    "models": _ModelsSettings,
    "sdk": _SdkSettings,
    "messaging": _MessagingSettings,
    "email": _EmailSettings,
    "artifacts": _ArtifactsSettings,
    "publication": _PublicationSettings,
    "mcp": _McpSettings,
    "evolution_fuel": _EvolutionFuelSettings,
}


#: The allowlist of recognised ``[sdk.pricing.<model>]`` rate keys that
#: :func:`_parse_pricing` checks each key against (the SDK's ``Pricing``
#: constructor defaults any absent key to ``0.0``).
_PRICING_RATE_KEYS = ("input", "output", "cache_creation", "cache_read")


def _strip_sdk_nonflat_keys(sdk_raw: dict[str, Any]) -> dict[str, Any]:
    """Return the ``[sdk]`` dict without the non-flat keys ``_SdkSettings`` omits.

    ``pricing`` / ``cli_paced`` / every ``<phase>_provider`` key are parsed by the
    dedicated helpers (:func:`_parse_pricing` / :func:`_parse_cli_paced` /
    :func:`_parse_phase_providers`), so they are removed before ``_SdkSettings``
    (now ``extra="forbid"``) sees the dict — leaving only genuinely-unknown
    ``[sdk]`` keys to trip the forbid guard. The base ``provider`` key is kept
    (it is a declared field): only ``<phase>_provider`` keys end with
    ``_provider``. A shallow copy is returned so the caller's ``sdk_raw`` — reused
    below for the three sub-table parsers — is left intact.
    """
    return {
        key: value
        for key, value in sdk_raw.items()
        if key not in ("pricing", "cli_paced") and not key.endswith("_provider")
    }


def _parse_phase_providers(sdk_raw: dict[str, Any]) -> dict[str, str]:
    """Collect ``[sdk] <phase>_provider`` overrides into ``sdk_phase_providers``.

    Every ``*_provider`` key other than the base ``provider`` folds in unfiltered,
    so a typo'd phase reaches
    :meth:`~maestro.config.Config._check_cross_field_and_subtable`'s unknown-phase
    raise and fails loud at load rather than being dropped here.
    """
    providers: dict[str, str] = {}
    for key, value in sdk_raw.items():
        if key == "provider" or not key.endswith("_provider"):
            continue
        providers[key[: -len("_provider")]] = str(value)
    return providers


def _is_number(value: object) -> bool:
    """True for an ``int``/``float`` TOML value that is not a ``bool``."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole_int(key: str, value: float) -> int:
    """Return *value* as ``int``, raising ``ValueError`` unless it is a whole finite number.

    ``int()`` would silently truncate ``100.5`` and raise ``OverflowError`` on ``inf``.
    """
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(
            f"[sdk.cli_paced] {key!r} must be a whole number, got {value!r}"
        )
    return int(value)


def _parse_pricing(sdk_raw: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Parse the ``[sdk.pricing.*]`` nested-table group into ``{model: rates}``.

    Hand-parsed rather than delegated to a pydantic model, which would coerce a
    numeric-looking string such as ``"2.0"``. Fail-loud: a non-table ``pricing``
    or model entry, a rate key outside ``_PRICING_RATE_KEYS``, or a rate that is a
    ``bool`` or non-number raises ``ValueError`` naming the offender, so a
    misspelt rate cannot silently price at ``0.0``. An absent rate key is
    legitimate (the SDK's ``Pricing`` defaults it to ``0.0``); a model entry
    contributing no rate is dropped. Returns ``{}`` when no pricing table is
    present.
    """
    if "pricing" not in sdk_raw:
        return {}
    pricing_section = sdk_raw["pricing"]
    if not isinstance(pricing_section, dict):
        raise ValueError("[sdk] pricing must be a table of [sdk.pricing.<model>]")
    table: dict[str, dict[str, float]] = {}
    for model_name, rates in pricing_section.items():
        if not isinstance(rates, dict):
            raise ValueError(f"[sdk.pricing] {model_name!r} must be a table of rates")
        clean: dict[str, float] = {}
        for key, value in rates.items():
            if key not in _PRICING_RATE_KEYS:
                raise ValueError(
                    f"[sdk.pricing.{model_name!r}] has unknown rate key {key!r}; "
                    f"valid keys: {', '.join(_PRICING_RATE_KEYS)}"
                )
            if not _is_number(value):
                raise ValueError(
                    f"[sdk.pricing.{model_name!r}] rate {key!r} must be a number, "
                    f"got {value!r}"
                )
            clean[key] = float(value)
        if clean:
            table[model_name] = clean
    return table


def _parse_cli_paced(sdk_raw: dict[str, Any]) -> dict[str, Any]:
    """Parse the ``[sdk.cli_paced]`` sub-table into the conditional ``Config`` fields.

    ``probe_metrics_enabled`` must be a bool and ``marker_pack`` a string;
    every other key must be a number: ``stream_idle_timeout_seconds`` /
    ``probe_metrics_every`` route to their own scalars and every
    ``VALID_CLI_PACED_CADENCE_KEYS`` key folds into a cadence dict (``cols`` /
    ``rows`` as ``int``, the rest as ``float``). A non-table ``cli_paced``, a
    wrongly-typed value, or an unknown key raises ``ValueError`` naming it; range
    and finiteness checks stay in
    :meth:`~maestro.config.Config._check_cross_field_and_subtable`. Keys are
    emitted only when present, so an absent table leaves every ``Config``
    cli_paced field at its dataclass default.
    """
    overrides: dict[str, Any] = {}
    if "cli_paced" not in sdk_raw:
        return overrides
    section = sdk_raw["cli_paced"]
    if not isinstance(section, dict):
        raise ValueError("[sdk] cli_paced must be a table")
    cadence: dict[str, float] = {}
    for key, value in section.items():
        if key == "probe_metrics_enabled":
            if not isinstance(value, bool):
                raise ValueError(
                    f"[sdk.cli_paced] probe_metrics_enabled must be a bool, "
                    f"got {value!r}"
                )
            overrides["sdk_cli_paced_probe_metrics_enabled"] = value
            continue
        if key == "marker_pack":
            if not isinstance(value, str):
                raise ValueError(
                    f"[sdk.cli_paced] marker_pack must be a string, got {value!r}"
                )
            overrides["sdk_cli_paced_marker_pack"] = value
            continue
        if not _is_number(value):
            raise ValueError(
                f"[sdk.cli_paced] {key!r} must be a number, got {value!r}"
            )
        if key == "stream_idle_timeout_seconds":
            overrides["sdk_cli_paced_stream_idle_timeout_seconds"] = float(value)
        elif key == "probe_metrics_every":
            overrides["sdk_cli_paced_probe_metrics_every"] = _whole_int(key, value)
        elif key in VALID_CLI_PACED_CADENCE_KEYS:
            cadence[key] = (
                _whole_int(key, value)
                if key in INT_CLI_PACED_CADENCE_KEYS
                else float(value)
            )
        else:
            raise ValueError(
                f"[sdk.cli_paced] has unknown key {key!r}; valid keys: "
                "probe_metrics_enabled, marker_pack, stream_idle_timeout_seconds, "
                f"probe_metrics_every, {', '.join(sorted(VALID_CLI_PACED_CADENCE_KEYS))}"
            )
    if cadence:
        overrides["sdk_cli_paced_cadence"] = cadence
    return overrides


def build_config_via_pydantic(
    config_dir: Path, env_name: str | None = None
) -> Config:
    """Build a :class:`~maestro.config.Config` through the declarative pydantic parse.

    Sources ``maestro.toml`` and an optional ``maestro.{env}.toml`` overlay,
    merges them with :func:`~maestro.toml_config._deep_merge`, rejects any unknown
    top-level section, validates each section through its ``pydantic-settings``
    model, parses the non-flat ``[sdk]`` sub-structures, reads env-var secrets,
    applies ``~`` / relative-path resolution and empty-string ``-> None``
    coercions, materialises a ``Config``, and runs
    :meth:`~maestro.config.Config._check_cross_field_and_subtable` on it so the
    cross-field and ``[sdk]`` sub-table guards the ``Field(...)`` constraints
    cannot express are enforced on the load path too (raising ``ValueError`` on
    a violation).
    """
    base_path = config_dir / "maestro.toml"
    if not base_path.exists():
        raise FileNotFoundError(
            f"Base config file not found: {base_path}\n"
            "maestro.toml must be present in the project root."
        )
    merged: dict[str, Any] = _read_toml(base_path)
    if env_name:
        if not _ENV_NAME_RE.match(env_name):
            raise ValueError(
                f"Invalid MAESTRO_ENV value: {env_name!r}. "
                "Must contain only alphanumeric characters, hyphens, and "
                "underscores."
            )
        overlay = _read_toml(config_dir / f"maestro.{env_name}.toml")
        merged = _deep_merge(merged, overlay)

    # Section-level analogue of ``_Section``'s per-key ``extra="forbid"``: a
    # top-level table that maps to no declarative model and is not a known
    # external section (today ``[review]``) fails loud at load, replacing the
    # legacy loader's silent drop-and-warn of an unrecognised section (a typo
    # like ``[looop]``) with a hard error on the authoritative path.
    unknown_sections = set(merged) - set(_SECTION_MODELS) - _KNOWN_EXTERNAL_SECTIONS
    if unknown_sections:
        raise ValueError(
            "Unknown top-level TOML section(s): "
            f"{', '.join(sorted(unknown_sections))}. Every section must map to a "
            "declarative model in _SECTION_MODELS or be a known external section "
            f"({', '.join(sorted(_KNOWN_EXTERNAL_SECTIONS))})."
        )

    flat: dict[str, Any] = {}
    for section, model_cls in _SECTION_MODELS.items():
        section_data = merged.get(section, {})
        if section == "sdk":
            # Strip the three hand-parsed non-flat keys so ``extra="forbid"``
            # rejects only genuinely-unknown ``[sdk]`` keys, not the sub-tables.
            section_data = _strip_sdk_nonflat_keys(section_data)
        flat.update(model_cls(**section_data).model_dump())

    # ``sdk`` is a required section, so a successful section loop guarantees it is
    # a table here; the three sub-structures below are optional within it.
    sdk_raw = merged.get("sdk", {})
    phase_providers = _parse_phase_providers(sdk_raw)
    pricing = _parse_pricing(sdk_raw)
    cli_paced = _parse_cli_paced(sdk_raw)

    workspace = Path(flat["workspace"]).expanduser()
    if not workspace.is_absolute():
        workspace = Path.cwd() / workspace
    memory_dir = Path(flat["memory_dir"]).expanduser()
    if not memory_dir.is_absolute():
        memory_dir = Path.cwd() / memory_dir
    # Host-scoped by default (``~/.maestro/gate_slots``); ``expanduser`` alone,
    # like the two paths above but without the cwd-relative rebase, so an operator
    # can point every checkout on a host at one shared slot set.
    gate_lease_slot_dir = Path(flat["gate_lease_slot_dir"]).expanduser()

    kwargs: dict[str, Any] = dict(flat)
    kwargs["workspace"] = workspace
    kwargs["memory_dir"] = memory_dir
    kwargs["gate_lease_slot_dir"] = gate_lease_slot_dir
    kwargs["github_repo"] = flat["github_repo"] or None
    kwargs["github_reviewer"] = flat["github_reviewer"] or None
    kwargs["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN")
    kwargs["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or None
    kwargs["dashboard_token"] = os.environ.get("MAESTRO_DASHBOARD_TOKEN")
    kwargs["email_address"] = os.environ.get("MAESTRO_EMAIL_ADDRESS")
    kwargs["email_password"] = os.environ.get("MAESTRO_EMAIL_PASSWORD")
    kwargs["email_thomas"] = os.environ.get("MAESTRO_EMAIL_THOMAS")
    kwargs["sdk_phase_providers"] = phase_providers
    if pricing:
        kwargs["sdk_pricing"] = pricing
    kwargs.update(cli_paced)
    config = Config(**kwargs)
    # The section-model ``Field(...)`` constraints validated every flat scalar
    # above; the cross-field/relational, semantic single-field, and hand-parsed
    # ``[sdk]`` sub-table guards live only in this shared method, so the
    # declarative build enforces the *complete* rejection surface on its own.
    config._check_cross_field_and_subtable()
    return config


def build_config_authoritative(
    config_dir: Path, env_name: str | None = None
) -> Config:
    """Build the declarative :class:`~maestro.config.Config` — the sole load-path validator.

    The entry point :meth:`Config.from_env` calls. Returns
    :func:`build_config_via_pydantic`'s result directly: that build now enforces
    the *complete* rejection surface itself — the section-model ``Field(...)``
    constraints for every flat scalar plus
    :meth:`~maestro.config.Config._check_cross_field_and_subtable` for the
    cross-field/relational and hand-parsed ``[sdk]`` sub-table guards — so the
    legacy ``Config.validate`` raise-ladder is no longer on the load path (it
    survives only for direct dataclass construction). A
    ``pydantic.ValidationError`` raised while building a section model is
    re-raised as a plain ``ValueError`` so ``from_env`` honours its "raises
    ``ValueError`` on bad config" contract (the cross-field guard already raises
    ``ValueError`` directly).
    """
    try:
        config = build_config_via_pydantic(config_dir, env_name)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    return config
