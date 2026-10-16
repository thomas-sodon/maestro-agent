"""Configuration loaded from TOML files and environment variables.

Priority chain: maestro.toml (committed defaults) < maestro.{env}.toml

Only secrets remain as environment variables:
  ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MAESTRO_DASHBOARD_TOKEN

All other settings (paths, timeouts, loop, limits) are TOML-only.
MAESTRO_ENV selects the TOML overlay file.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from maestro import model_profiles
from maestro.work_target import WorkFraming, WorkTarget

logger = logging.getLogger(__name__)

# Valid values for ``[sdk] provider`` (proposal 058). Forward-compat list of
# *names the config schema accepts*; the authoritative registered-name check is
# :func:`maestro.providers.registry.configure_provider` at ``cli.main`` time.
# Selecting a not-yet-registered name passes this schema check but fails loud in
# ``configure_provider`` — the correct fail-loud posture, and inert under the
# "anthropic" default. Kept here (not in the registry) because the lightweight
# ``Config.validate()`` path must not import the agent SDK.
VALID_SDK_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "cli_paced"})

# Planner callers ``[loop] grounded_planning_callers`` may name (proposal 143):
# the ones wired onto the bounded ``tree`` surface.
GROUNDED_PLANNING_CALLERS: frozenset[str] = frozenset(
    {"propose_plan", "propose_plan_chain", "curate_pending_plans"},
)

# Valid values for the two ``[work]`` framing fields (proposal 107). ``self_evolution``
# is today's behaviour (Maestro works on itself); ``external_delivery`` points the
# loop at another repo. Kept alongside ``VALID_SDK_PROVIDERS`` because the same
# lightweight ``Config.validate()`` path checks it and must not import heavier
# modules. The ``Literal`` alias lives in :mod:`maestro.work_target`.
VALID_WORK_FRAMINGS: frozenset[str] = frozenset(
    {"self_evolution", "external_delivery"}
)

# Phase names accepting a per-phase ``[sdk] <phase>_provider`` override
# (proposal 058). A ``*_provider`` key naming a phase outside this allowlist
# fails at load instead of silently creating a dead override. Mirrors the
# loop's session callers in :mod:`maestro.agent`.
SDK_PHASE_NAMES: frozenset[str] = frozenset({
    "contemplation",
    "evolution",
    "implementation",
    "task_generation",
    "artifact_review",
    "rejection",
    "run_task",
})

# Phases that must be able to execute work for the loop to make progress —
# the task-execution paths where a provider with an unusable credential cannot
# run at all (proposal 067 Step 1). A per-phase provider routed here gets a
# hard credential check; one routed only elsewhere degrades to a warning.
# Same membership as ``tool_surface.action_gated_phases()`` today, but a
# separate set on purpose: "must execute" is an operational fact, "mutates the
# repo" is a trust fact, and the two could diverge.
MUTATING_SDK_PHASE_NAMES: frozenset[str] = frozenset({
    "implementation",
    "run_task",
})

# Valid ``[sdk.cli_paced]`` Cadence override knobs (proposal 056 Step 12). These
# are the numeric ``maestro_sdk.Cadence`` fields the cli_paced provider lets an
# operator tune; an absent key keeps its Cadence default. Hardcoded (not imported
# from ``maestro_sdk``) so the lightweight ``Config.validate()`` path stays free of
# the ``maestro_sdk`` import — a guard test pins this set ⊆ ``Cadence``'s fields so
# the two cannot drift. ``stream_idle_timeout_seconds`` is *not* here: it is a
# ``QueryOptions`` deadline, carried in its own ``Config`` field.
VALID_CLI_PACED_CADENCE_KEYS: frozenset[str] = frozenset({
    "mean_key_ms",
    "stddev_key_ms",
    "min_key_ms",
    "word_pause_ms",
    "sentence_pause_ms",
    "thinking_pause_prob",
    "heartbeat_min_ms",
    "heartbeat_per_answer_char_ms",
    "heartbeat_max_ms",
    "cols",
    "rows",
    "size_jitter",
})

# The subset of ``VALID_CLI_PACED_CADENCE_KEYS`` that are ``int`` (not ``float``)
# fields on ``maestro_sdk.Cadence`` (proposal 056 Step 12). ``validate()`` requires
# integral values for these so a fractional ``cols``/``rows`` fails at config load
# rather than later, when it would reach the pty winsize as a float. Lives here (not
# in ``toml_config``) so ``toml_config`` imports it the same direction
# ``VALID_CLI_PACED_CADENCE_KEYS`` flows (config → toml_config), avoiding a cycle.
INT_CLI_PACED_CADENCE_KEYS: frozenset[str] = frozenset({"cols", "rows"})

# The subset of ``VALID_CLI_PACED_CADENCE_KEYS`` that name a unit-interval [0, 1]
# knob on ``maestro_sdk.Cadence`` (proposal 056 Step 12). ``validate()`` rejects a
# value > 1 for these so a typo'd probability fails at config load rather than
# silently degrading pacing: ``thinking_pause_prob`` is compared against
# ``random()`` (a value > 1 makes *every* keystroke take a thinking pause), and
# ``size_jitter`` drives a ±jitter factor (a value > 1 can swing the pty winsize
# negative before the 20-col/8-row floor clamps it). The lower bound is already
# covered by the shared non-negative gate.
UNIT_INTERVAL_CLI_PACED_CADENCE_KEYS: frozenset[str] = frozenset({
    "thinking_pause_prob",
    "size_jitter",
})

# Serena's file-write, code/symbol-edit, memory-write, and shell-exec tools,
# denied by qualified ``mcp__serena__*`` name when the server is enabled so it
# stays a read-only retrieval surface. Scoped to the pinned
# ``serena-agent==1.5.3`` inventory (see ``serena_mcp_args``): every tool that
# pin exposes whose job is to run shell, write file content, edit code/symbols,
# or read-modify-write a memory is named below, grouped by class.
# ``execute_shell_command`` is the critical one — it could run ``gh pr create`` /
# ``git push`` and bypass the ``Bash``-scoped action gate — but every file/symbol
# writer is denied too, since the first-cut seam exposes read-only retrieval only
# (scoped symbol edits are out of scope, proposal 084).
#
# Deliberately NOT denied (they change only Serena's own runtime/registry state,
# not the target repo, its memories, or the action gate): Serena's
# project-lifecycle / workflow tools ``activate_project`` (in fact *required* to
# point the server at a repo, so denying it would break retrieval), ``onboarding``,
# ``remove_project`` and ``restart_language_server``. The JetBrains-backend editors
# (``move`` / ``rename`` / ``safe_delete`` / ``inline_symbol``) are not exposed
# under the standalone ``start-mcp-server`` launch. ``replace_regex`` is retained
# defensively — 1.5.3 exposes the equivalent as ``replace_content`` (also denied),
# so the older name is a harmless belt-and-suspenders entry.
#
# Because this is a deny-list, any tool it omits passes through: any new tool in a
# bumped Serena inventory MUST be classified into one of the denied classes (and
# named here when it writes/execs) before the pin is bumped, so inventory drift
# fails loudly rather than silently re-opening the gate.
SERENA_MUTATING_TOOLS: tuple[str, ...] = (
    "execute_shell_command",
    # File-content writers.
    "create_text_file",
    "replace_regex",
    "replace_content",
    "delete_lines",
    "replace_lines",
    "insert_at_line",
    # Code / symbol editors.
    "replace_symbol_body",
    "rename_symbol",
    "safe_delete_symbol",
    "insert_after_symbol",
    "insert_before_symbol",
    # Memory read-modify-write.
    "write_memory",
    "edit_memory",
    "rename_memory",
    "delete_memory",
)

# Each MCP launcher binary ships inside a larger toolchain rather than as a
# standalone package of the same name: ``uvx`` ships with ``uv``; ``npx`` ships
# with Node.js. Map each known launcher to an install hint that names the
# toolchain to provision, so a missing-launcher error points at the right thing
# to install (the launcher name itself still appears in the message). Unknown
# launchers fall back to a generic ``install <command>``.
MCP_LAUNCHER_INSTALL_HINTS: dict[str, str] = {
    "uvx": "install uv (which provides uvx; https://docs.astral.sh/uv/)",
    "npx": "install Node.js (which provides npx)",
}

# The external (launcher-backed subprocess) MCP servers, keyed by config prefix.
# Each has ``{prefix}_mcp_enabled`` / ``_mcp_command`` / ``_mcp_args`` fields and
# is started by an ``npx``/``uvx`` launcher; the in-process introspection,
# email, and telegram servers are launcher-less and never appear here. Single
# source of truth for the ``_validate`` loop below and the Dockerfile
# config↔image parity guard in ``tests/test_dockerfile_structure.py``, so a new
# external server is added in one place rather than silently escaping either.
EXTERNAL_MCP_PREFIXES: tuple[str, ...] = ("context7", "serena")


@dataclass
class Config:
    """Maestro configuration.

    Production defaults are defined in maestro.toml (committed to repo).
    Use ``Config.from_env()`` to load from TOML + environment variables.

    Dataclass field defaults exist for test convenience only.
    """

    workspace: Path = field(default_factory=lambda: Path(os.getcwd()))
    memory_dir: Path = field(default_factory=lambda: Path.home() / ".maestro")
    # SQLite state-store substrate (proposal 106). When true, the mutable
    # candidate-task queue, the per-SDK-call cost samples, the deed ledger, the
    # owned-backlog snapshot ledger, and the PR-admission provenance ledger all
    # round-trip through ``memory_dir / "state.db"`` (via ``SqliteTaskQueue`` /
    # ``SqliteCostSampleStore`` / ``SqliteDeedLedger`` /
    # ``SqliteOwnedBacklogLedger`` / ``SqlitePrProvenanceLedger``) instead of
    # their sidecars (``task_queue.json`` / ``cost_samples.jsonl`` /
    # ``deed_ledger.jsonl`` / ``owned_backlog_ledger.jsonl`` /
    # ``pending_pr_provenance.jsonl``); the sidecars are never deleted, so
    # flipping this back off is a clean rollback. Default off — production keeps
    # the sidecar paths untouched.
    sqlite_state_enabled: bool = False
    github_repo: str | None = None
    github_reviewer: str | None = None
    github_issues_page_size: int = 5
    verbose: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    dashboard_port: int = 8420
    dashboard_host: str = "0.0.0.0"
    dashboard_token: str | None = None
    trust_forwarded_proto: bool = False
    # When True, rate-limit keys read ``X-Real-IP`` set by nginx
    # (``proxy_set_header X-Real-IP $remote_addr`` — unspoofable).
    # Dev default is False because a dashboard bound to 127.0.0.1 has no
    # trusted proxy in front of it.
    trust_forwarded_for: bool = False
    # Public-API substrate. Rate-limit and cache-control values for every
    # ``/api/public/*`` endpoint — see ``maestro/dashboard/public_api.py``.
    public_api_rate_limit_per_minute: int = 60
    public_api_cache_max_age_seconds: int = 30
    # Gates ``SecurityHeadersMiddleware``. Off-switch exists for operators
    # debugging a CSP-breaking page without ripping the middleware out.
    security_headers_enabled: bool = True
    # Authenticated-mutation rate limits (proposal 022) — per resolved
    # client IP, sliding 60 s window, applied by MutationRateLimitMiddleware
    # only when dashboard auth is enabled.
    login_rate_limit_per_minute: int = 10
    approval_rate_limit_per_minute: int = 30

    # Timeouts (seconds)
    git_timeout: int = 30
    git_quick_timeout: int = 10
    selfcheck_timeout: int = 900
    # Hard cap on a lease-derived pytest budget (``gate_budget_lease_owned_enabled``).
    # ``selfcheck_timeout`` stays the ruff/mypy ceiling and the derived deadline's
    # floor; this bounds the escalation so a genuine hang still dies. Guarded
    # ``>= selfcheck_timeout`` in ``_check_cross_field_and_subtable``.
    selfcheck_max_timeout: int = 1350
    implement_timeout: int = 120
    pr_creation_timeout: int = 60
    gh_timeout: int = 30
    shutdown_timeout: float = 10.0

    # Dashboard limits
    rejection_reason_max: int = 2000
    max_page_size: int = 500
    log_buffer_size: int = 500
    # Gates the v2 account-dashboard navigation shell (proposal 122). Graduated
    # to the base default 2026-09-03: the /account handler stamps a
    # data-account-ui-v2 marker onto <body> that the grouped sidebar CSS/JS
    # branch on, so an operator gets the accessible console with no config edit.
    # Setting it false is the one-line rollback for the burn-in window — the flat
    # pill nav returns and the served markup is byte-identical to the template.
    account_ui_v2: bool = True

    # Loop
    loop_interval: int = 60
    loop_evolve_every: int = 5
    loop_task_every: int = 3
    # Soft cap on the candidate-task queue maintained by ``generate_task``.
    # The phase stops enqueueing new items once the on-disk queue reaches
    # this size; it never evicts existing entries behind the LLM's back.
    task_queue_max: int = 8
    loop_dream_every: int = 10
    # Mechanical self-audit cadence — runs the deterministic rules in
    # :mod:`maestro.audit_checks` against the last 20 tasks + current
    # self-model every N iterations. 0 disables the phase entirely.
    loop_audit_self_every: int = 10
    # Priced-introspection cadence (proposal 113). ``contemplate`` issues two
    # high-effort token-spending SDK calls (priced ``introspect`` +
    # ``update_self_model``) every time it runs; this knob lets an operator run
    # it once every N iterations instead of every one. The default of ``1`` is
    # exactly today's every-iteration behaviour; ``0`` disables the phase.
    loop_contemplate_every: int = 1
    # Pending-plan curation cadence (proposal 060). Opt-in by design: the
    # ``curate_pending_plans`` phase lets an autonomous LLM rewrite a stale
    # ``pending_review`` plan's body before Thomas reviews it, so it stays
    # dark unless BOTH ``loop_curate_enabled`` is true AND
    # ``loop_curate_every`` is a positive cadence. ``0`` (the default)
    # disables the phase entirely.
    loop_curate_every: int = 0
    loop_curate_enabled: bool = False
    # Grace period (float hours) a fresh ``pending_review`` plan must age past
    # before ``curate_pending_plans`` may touch it. ``select_curation_target``
    # drops any plan younger than this floor, so an operator is not shown a
    # freshly-minted plan rewritten on the very next curate tick. ``0.0`` (the
    # default) is a no-op — today's immediate behaviour. Curation of an already
    # mature backlog is unchanged; only the "refined moments after creation"
    # case is closed.
    loop_curate_min_plan_age_hours: float = 0.0
    # Proposal 145: when true, ``select_curation_target`` curates a pending
    # plan carrying unaddressed reviewer feedback first, past the grace floor.
    loop_plan_feedback_enabled: bool = False
    # When true, ``select_curation_target`` excludes from the normal
    # (non-feedback) curation lanes every plan already serviced once —
    # ``refined_at`` OR ``last_curated_at`` not ``None`` — so only
    # never-serviced plans are curation-eligible. The ``curated_hash`` skip
    # (proposal 155 step 6) already drops an unchanged serviced plan; this
    # flag also keeps a serviced plan out after its content or feedback
    # changes. The reviewer-feedback priority lane is exempt.
    loop_curate_skip_serviced_enabled: bool = False
    # Duplicate-pending-plan guard threshold. Before persisting a fresh
    # ``pending_review`` plan, ``submit_for_review`` refuses to mint one
    # that near-restates a plan Thomas has not yet ruled on, reusing the
    # zero-LLM-cost content-word Jaccard machinery behind
    # ``detect_restatement`` at this cutoff (mirrors
    # ``RESTATEMENT_THRESHOLD``). Raise toward 1.0 to require closer
    # matches; set ``> 1.0`` to disable the guard (no overlap can reach it).
    loop_duplicate_pending_plan_threshold: float = 0.5
    # Cluster-level themed-restatement guard threshold. The pairwise
    # ``duplicate_pending_plan_threshold`` only sees plan pairs, so a backlog
    # of near-restatements (each below the Jaccard cutoff, all meaning the
    # same thing) can accumulate one plan at a time. ``submit_for_review``
    # additionally consults ``find_theme_cluster_pending_plans``: when at
    # least this many open ``pending_review`` plans share the candidate's
    # dominant-token theme, the candidate lands ``obsolete`` instead of
    # minting a fresh review burden. Raise to tolerate larger themed
    # backlogs; ``<= 1`` would block on a single themed match (aggressive).
    loop_pending_plan_cluster_threshold: int = 2
    # Cluster-level pending-plan stop. Where the duplicate guard above blocks
    # a near-restatement of a *single* pending plan, this stops theme-cluster
    # proliferation: variants that each sit just below the strict duplicate
    # threshold yet collectively form one theme choking the review queue.
    # ``find_pending_plan_cluster`` counts every ``pending_review`` plan whose
    # content-word Jaccard against the candidate meets
    # ``loop_pending_cluster_threshold`` (a *looser* cutoff than the duplicate
    # threshold above); when that count reaches ``loop_pending_cluster_max``,
    # ``submit_for_review`` lands the incoming plan ``obsolete`` instead of
    # minting a new ``pending_review`` row. Set ``loop_pending_cluster_max <= 0``
    # to disable the cluster stop entirely.
    loop_pending_cluster_threshold: float = 0.35
    loop_pending_cluster_max: int = 2
    # Mechanical pending-plan cluster sweep. When true, ``generate_task``
    # runs ``Memory.sweep_clustered_pending_plans()`` at the top of each
    # cycle: a zero-SDK, no-approval pass that reuses the cluster detectors
    # behind the submit-time guard (``loop_pending_cluster_threshold`` /
    # ``loop_pending_cluster_max``) to mark all-but-the-newest member of
    # every qualifying ``pending_review`` cluster ``obsolete`` via the owned
    # terminal act ``mark_plan_obsolete``. Drains a self-fed backlog without
    # routing the choice through a per-pop gate. Set false to disable.
    loop_pending_cluster_sweep_enabled: bool = True
    # Mechanical pending-plan *restatement* sweep — the pairwise complement of
    # the cluster sweep above. When true, ``generate_task`` also runs
    # ``Memory.sweep_restatement_pending_plans()``: a zero-SDK, no-approval
    # pass that marks every ``pending_review`` plan restating a *strictly-newer*
    # surviving pending plan ``obsolete`` (keeping the newest of each chain),
    # capturing each fire's literal return to ``lever_attempts.jsonl``. Reuses
    # ``loop_duplicate_pending_plan_threshold`` as the Jaccard cutoff — no new
    # knob. Catches pairwise restatement chains that never form a cluster of
    # ``loop_pending_cluster_max`` members. Set false to disable.
    loop_restatement_sweep_enabled: bool = True
    # Directive-named obsolete actuator. When true, the zero-SDK
    # ``audit_self_model`` phase fires ``mark_plan_obsolete`` on the single
    # ``pending_review`` plan a ``next_task_directive`` names alongside that
    # lever (via ``lever_capture.fire_directive_named_obsolete``), records an
    # ``OWNED_PRUNE`` deed, and clears the directive it acted on — or clears a
    # directive whose single named plan already left ``pending_review``. Runs
    # regardless of ``self_directed_tasks_enabled``. Default off pending burn-in.
    loop_obsolete_directive_actuator_enabled: bool = False
    # Concept-key restatement matching. When true, the self-model restatement
    # gate (``detect_restatement`` via ``update_self_model``) compares content
    # tokens folded through ``lexicon.concept_key`` — hyphenation and light
    # morphology collapse — so a fact re-coined under a fresh hyphenated handle
    # is caught as a restatement instead of scored as a discovery. Default off:
    # every current restatement decision is byte-for-byte unchanged until set.
    loop_restatement_concept_key_enabled: bool = False
    # Backlog-saturation submit-block. The cluster/duplicate guards above cap
    # *redundant* production (near-restatements of what already sits pending);
    # this caps *total* production regardless of content. Before persisting a
    # fresh ``pending_review`` row, ``submit_for_review`` counts the open
    # ``pending_review`` backlog and hard-refuses the new plan once that count
    # reaches this threshold — the only forward path becomes landing or
    # disposing existing plans first, binding the machinery against
    # manufacturing more intentions than the reviewer can absorb. Set
    # ``<= 0`` to disable the production cap entirely.
    loop_pending_plan_submit_block_threshold: int = 5
    # Age-based pending-plan expiry horizon. The cluster/restatement sweeps
    # retire only *redundant* pending plans; a genuinely-unique plan that is
    # never reviewed rots indefinitely against a moving tree. When positive,
    # ``generate_task``'s per-cycle drain also runs
    # ``Memory.sweep_expired_pending_plans()``: a zero-SDK, no-approval pass
    # that marks every ``pending_review`` plan older than this many days
    # ``obsolete`` (disposal, not rejection — an approval moves a plan out of
    # ``pending_review`` and is the explicit hold). Set ``<= 0`` to disable.
    loop_plan_review_expiry_days: int = 21
    loop_max_replans: int = 1
    # Consecutive-remediation cap. ``_run_task_phase`` selects task goals in
    # priority order ``pending_user_requests`` → ``pending_pr_requests`` →
    # queue head; because ``pending_pr_requests`` bypasses ``task_every`` and
    # the tend phase re-feeds the same unlanded PRs every iteration, an
    # unbounded streak of remediation slots can consume every task slot
    # (remediation-monoculture). This caps how many *consecutive* slots may
    # be served from ``pending_pr_requests`` before one slot is forced to
    # fall through to the queue head, restoring task-shape diversity. ``<= 0``
    # disables the cap and preserves remediation's unconditional precedence.
    max_consecutive_remediation: int = 4
    post_implement_review: bool = True
    # Task-branch reuse as an evolution-PR base (proposal 116). When a plan
    # nominates a kept task branch via ``source_task_id``, ``implement`` may
    # base the evolution branch on that branch (after merging ``origin/main``
    # into it) instead of on ``main``. This master switch defaults ``True``
    # (today's behaviour). Flag-off skips the ``source_task_id`` checkout /
    # merge / fallback block entirely so every evolution PR is cut
    # fresh-from-``main`` — the production-proven default — and the PR body's
    # reuse-lineage line is suppressed. Inverted vs. a ``CutoverFlag``: the
    # default-on branch is the *old* behaviour being retired.
    task_branch_reuse_enabled: bool = True
    # Drop ``Remediate PR #<n>:`` laps from evolution planning's recent-task
    # window (``remediation_tracker.recent_planning_tasks``): a lap's kept
    # branch is an open PR head, not a promotable task branch. Default off.
    planning_excludes_remediation_enabled: bool = False
    # Self-directed task pipeline master switch (proposal 116). Defaults
    # ``True`` (today's behaviour). Flag-off makes the phase-cadence oracle
    # stop scheduling ``generate_task`` entirely, starving the self-directed
    # channel while ``run_task`` and every external / PR-remediation path
    # (``pending_user_requests`` / ``pending_pr_requests`` / task-queue drain)
    # keep running every iteration exactly as today.
    self_directed_tasks_enabled: bool = True
    # Evolution-plan admission gate (proposal 116, step 7). Default on since the
    # proposal-116 graduation: when ``True``, ``Evolution.submit_for_review`` refuses a net-new
    # mint when the pending backlog is saturated *or* the candidate's
    # self-measurement theme already dominates the recent obsolete graveyard,
    # recording it obsolete and writing one bounded prune/land directive. Off
    # restores the pre-graduation un-gated submission.
    generation_admission_enabled: bool = True
    # Themed fraction of the recent-obsolete window at/above which the
    # candidate's self-measurement theme "dominates". Validated to [0.0, 1.0].
    generation_admission_theme_fraction: float = 0.75
    # Size of the recent-obsolete window fed to the theme-saturation metric.
    # Validated >= 1.
    generation_admission_window: int = 20
    # Pre-mint generation gate (proposal 120, Phase B). Default-off master
    # switch: when ``True``, ``evolve()`` consults
    # ``maestro.generation_gate.may_mint_plan`` *before* spending the priced
    # ``propose_plan`` call, using the same backlog-depth and
    # theme-saturation inputs the submission-side admission gate reads; on a
    # refusal it skips the priced call and writes the same bounded prune/land
    # directive. Off (default) preserves today's behaviour byte-for-byte — the
    # submission-side ``generation_admission_enabled`` gate stays the sole
    # governor. Introduces no new threshold.
    pre_mint_generation_gate_enabled: bool = False
    # Self-model external-capability anchor (proposal 120, Phase C). Default-off:
    # when ``True``, ``update_self_model``'s ``limitations_known`` vanity filter
    # gains a fifth, *necessary* admission test — a limitation qualifies only if
    # it names an external-observable capability, behaviour, or audience effect,
    # not solely the loop's own plan-mint / PR-remediation plumbing — and calls
    # that inward-plumbing case out as vanity by example. Off (default) leaves the
    # ``update_self_model`` prompt byte-for-byte unchanged; the four existing
    # admission tests and the restatement / compound / ``MAX_LIST_ITEMS`` content
    # guards are untouched.
    self_model_external_anchor_enabled: bool = False
    # Age cap for the startup stale-checkpoint sweep (proposal 006). Task
    # checkpoints under ``{memory_dir}/checkpoints/`` older than this are
    # deleted on loop startup so orphans (from manually deleted branches or
    # schema drift) cannot accumulate. ``0`` disables the sweep.
    checkpoint_max_age_days: int = 7

    # Audit stall actuator — when an ``[audit]``-prefixed open question
    # survives this many consecutive ``audit_self_model`` cycles without
    # being INTEGRATEd, ``generate_task`` returns ``(None, None)`` (zero
    # SDK cost) until the LLM removes the entry. The threshold is in
    # *audit cycles*, not loop iterations: at default
    # ``loop_audit_self_every=10`` a threshold of ``5`` is ≈ 50 iterations
    # of grace. Opt-in: ``audit_stall_block_enabled=False`` keeps the
    # actuator dormant so we collect lived data before flipping the flag.
    audit_stall_threshold: int = 5
    audit_stall_block_enabled: bool = False

    # Stale-self-model-entry rule (rule 6) — when true, the audit phase
    # builds a rolling textual-evidence corpus and flags entries in
    # ``identity_statements`` / ``capabilities_known`` / ``values``
    # whose content tokens never appear in any single recent journal /
    # task / audit / resolution string. Enabled so subsequent
    # audit_self_model cycles detect stale entries automatically.
    audit_stale_entry_enabled: bool = True

    # Post-merge impact-review audit rule (rule 9) — closes the
    # verify→remove half of the full circuit. Scans the last
    # ``audit_impact_review_window`` ``VersionRecord`` entries and surfaces
    # an ``[audit] unreviewed_evolution`` finding for any merged evolution
    # older than ``audit_impact_review_after_days`` that no recent task has
    # revisited (by commit-SHA reference), so its acceptance criteria can be
    # re-validated or the evolution retired. A surfaced-set sidecar dedups
    # so a version is raised once. Default-on; zero SDK cost.
    audit_impact_review_enabled: bool = True
    audit_impact_review_after_days: int = 7
    audit_impact_review_window: int = 10

    # Standing settlement reconciler (proposal 097) — the master switch for the
    # zero-SDK reconciler that reads the trailing remediation verdicts, consults
    # the ``is_pr_settled`` chokepoint once per distinct PR, and records every
    # settled (merged *or* closed) PR to a durable ``settled_verdicts.jsonl``
    # ledger. On (the default): each audit cycle ``_audit_self_model`` binds the
    # trailing remediation verdicts to ``is_pr_settled`` and threads the settled
    # set into the two phantom rules as their ``settled_prs`` pre-filter, so a
    # settled PR is skipped before each rule's merge predicate and every phantom
    # finding fires at most once and then genuinely self-extinguishes — even
    # after its verdict record freezes in append-only history.
    settlement_reconciler_enabled: bool = True

    # On-settlement record reconciler (proposal 103) — the master switch for the
    # owner that drives a *registry* of small per-store reconcilers off the single
    # ``is_pr_settled`` settlement signal, clearing every PR-keyed trace when a PR
    # settles and recording each clear to a durable ledger. On (the default): the
    # five per-store reconcilers run at the merge transition and each audit cycle,
    # and the ``settlement_record`` structural guard (``tests/structural_guards.py``)
    # keeps ``reconcile_pr_settlement``'s registry the sole on-settlement
    # store-clear door.
    settlement_record_reconciler_enabled: bool = True

    # Orphaned-cure audit rule (rule 11) — the inward-turned tripwire. Flags an
    # own ``implemented``-but-unmerged evolution plan that has sat unmerged for
    # at least ``orphaned_cure_age_cycles`` audit cycles while remediation-shaped
    # tasks keep being lapped in the same window — i.e. a built cure orphaned at
    # the merge tail while the laps it would eliminate continue. Detection-only;
    # self-extinguishes once the cure merges (the plan leaves ``implemented``).
    # Default-on; zero SDK cost (one sidecar read/write under a file lock).
    orphaned_cure_enabled: bool = True
    orphaned_cure_age_cycles: int = 3

    # Orphaned-cure retire actuator — the mechanical dispose complement of
    # rule 11's detection. When enabled, the audit cycle retires (via
    # ``Memory.sweep_orphaned_cures``) an ``implemented``-but-unmerged plan
    # whose sidecar age reaches ``orphaned_cure_retire_age_cycles`` **and**
    # whose PR tend has directly observed as CONFLICTING
    # (``last_conflict_resolution_attempt_at`` set) — live conflict evidence,
    # not a stamp. The ceiling must be strictly greater than
    # ``orphaned_cure_age_cycles`` (10x the flag default) so only
    # truly-dead cures qualify; the PR itself stays open for the human.
    # Default-on; zero SDK cost (plan-store rewrite + one ledger append).
    orphaned_cure_retire_enabled: bool = True
    orphaned_cure_retire_age_cycles: int = 30

    # Phantom-escalation audit rule (rule 12) — the standing alarm at the door.
    # Flags ``partial`` remediation laps whose ``result_summary`` asserts
    # "escalating" for a ``#NNNN`` PR while no outbound message to Thomas
    # references it — escalation that lived only in prose. Self-extinguishes the
    # moment a referencing message is delivered. Default-on; zero SDK cost (pure
    # cross-reference of recent tasks against recent messages).
    phantom_escalation_enabled: bool = True

    # Rule-12 delivery arm (proposal 090) — the phantom-escalation rule's
    # completion face. When set, each qualifying rule-12 finding (a ``partial``
    # lap narrating "escalating" for a ``#NNNN`` across the ≥2-lap threshold with
    # no referencing message) is *completed* by ``deliver_claimed_escalation``:
    # one merge-or-close message to Thomas + a durable key-keyed standdown (aged
    # out by ``prune_expired_standdowns`` because a plain-issue key never merges),
    # so the narration's promised action is delivered rather than re-litigated.
    # Rule 12 then self-extinguishes because the message now references the
    # number. Nested inside the ``phantom_escalation_enabled`` master switch so
    # delivery requires detection. Default-off so it ships dark, matching the
    # other forced-disposition gates; no new push capability — the only owned act
    # is one message to Thomas. Zero SDK cost.
    phantom_escalation_delivery_enabled: bool = False

    # Unconfirmed-own-landing audit rule (rule 14) — the standing alarm at my own
    # commit. Flags a ``partial`` task whose ``result_summary`` carries an
    # on-branch handoff marker + a committed SHA + a ``#NNNN`` when git-merge-truth
    # shows that SHA is still not an ancestor of ``origin/main`` (and the PR has
    # not otherwise merged) past ``unconfirmed_landing_age_cycles`` audit cycles —
    # handed is not landed. Age is measured in cycles via a first-seen sidecar so
    # Tend's push latency is never mistaken for a gap; self-extinguishes the moment
    # the SHA lands. Default-on; zero SDK cost (one sidecar read/write plus bounded
    # git subprocesses per SHA/PR).
    unconfirmed_landing_enabled: bool = True
    unconfirmed_landing_age_cycles: int = 3

    # Rule-14 forced disposition (proposal 090) — the handoff tripwire's louder
    # face. When set, each past-grace
    # rule-14 finding whose statement cites a ``#NNNN`` is routed through the
    # durable ``pr_number``-keyed escalation ledger (one merge-or-close message +
    # a standdown auto-released on merge/close) instead of dead-ending as a
    # journal line. Nested under the rule-14 master switch so delivery requires
    # detection; reads the graced findings, so it inherits the same grace window
    # and per-rule cap. Default-off so it ships dark, matching the other
    # forced-disposition gates.
    # No new push capability — the only owned act is looking + one message.
    unconfirmed_landing_forced_enabled: bool = False

    # Mergeable-flip merge-request actuator (proposal: mechanically recruit the
    # lander at reconciliation). When set, the §5c-iv record path fires
    # ``outcome_grounding.bind_mergeable_flip_to_merge_request`` for a
    # PR-remediation verdict whose PR ``is_pr_settled`` shows clean-but-unlanded and
    # whose sole blocker ``classify_blocker_ownership`` reports external and
    # mergeable-terminal (AWAITING_APPROVAL — the human-approval gate; the wired
    # readiness caller never emits AWAITING_PUSH, Tend's mechanical job): it sends
    # exactly one named merge-request via ``deliver_handoff`` — the only landing to me
    # at a mergeable flip — keyed idempotent on a persisted ``(pr_number, head_sha)``
    # serviced marker. Default-off keeps Thomas's safety keystroke; enabling it
    # stops the merge-request depending on the intention I chronically fail to
    # execute. No new push capability — the only owned act is one message.
    mergeable_merge_request_enabled: bool = False

    # Frame-discharge costume cap (proposal 112) — the record-time actuator over the
    # self-directed frame↔discharge contract. When enabled, a completed self-directed
    # task whose goal is investigation-framed but whose discharge is tracked
    # construction (a kept branch, not ``artifacts/``-only) has its ``success`` capped
    # to ``partial`` with a ``[frame-discharge-mismatch]`` note, via
    # ``frame_discharge.assess_frame_discharge`` + ``bind_costume_discharge_to_partial``
    # in ``run``. Default-off keeps current behaviour until incidence is validated
    # against live task history; domain-disjoint from the §5d landing authority.
    frame_discharge_bind_enabled: bool = False

    # Ungrounded-question-closure audit rule (rule 16, proposal 088 Phase 1) —
    # the eyes-veto over a closure. Re-surfaces every ``removed`` question whose
    # only probe did not succeed (``QuestionResolution`` with disposition
    # ``removed`` + outcome != ``success``) as a ``contradicted_claim`` finding,
    # so the next ``update_self_model`` must re-open it or justify the closure.
    # Self-extinguishing once the row scrolls out of the window or a successful
    # probe re-resolves the question. Default-off for opt-in lived-data
    # validation; zero SDK cost (pure read of existing resolution rows).
    audit_ungrounded_closure_enabled: bool = False

    # Construction-dominance audit rule (rule 17) — surfaces the task-shape
    # *choice* that precedes false-landing claims. When the construction-shaped
    # fraction of the last ``construction_dominance_window`` tasks reaches
    # ``construction_dominance_threshold``, the rule emits exactly one finding
    # naming the over-selection of a shape whose verdict Maestro signs itself
    # (it never delivers an external "no"). Self-extinguishes the moment the mix
    # diversifies below the threshold. Default-off for opt-in lived-data
    # validation; zero SDK cost (pure regex classification of recent goals).
    construction_dominance_enabled: bool = False
    construction_dominance_threshold: float = 0.6
    construction_dominance_window: int = 10

    # Phantom-partial-prune audit rule (rule 19) — the affirmative counterpart to
    # the git-keyed landing binds, aimed at the *under*-claim side of
    # the grounding-drift loop. When a ``partial``-stamped self-directed,
    # ``pr_head_sha``-less task fired ``mark_plan_obsolete`` on an extractable
    # 12-hex plan_id whose disk status reads ``obsolete`` (a fresh
    # ``evolution_plans.jsonl`` load — the target plan_id's status transition,
    # not the gross ``pending_review`` count-delta concurrent writes corrupt),
    # the rule emits one finding: the prune landed, the verdict under-claimed it.
    # Self-extinguishes the moment no such mismatch exists. Default-off for
    # opt-in lived-data validation per the audit-rule convention; zero SDK cost
    # (bounded reads of task + plan records, no git oracle).
    phantom_partial_prune_enabled: bool = False

    # Under-credited-landing audit rule (rule 20) — the affirmative twin of rule
    # 14 (unconfirmed-own-landing). Where rule 14 flags a ``partial`` handoff
    # whose committed SHA git shows is *not* an ancestor of ``origin/main``
    # (over-claim), this flags a non-``success`` task whose own-committed SHA
    # (``extract_self_committed_shas``) git *proves* IS an ancestor — a landing
    # authored and stamped nothing. It closes the sensor asymmetry that trains
    # the disposal treadmill: every other landing rule can only downgrade a
    # claim, none rewards a confirmed one. Not an auto-relabel — the ``partial``
    # may be correct for other goals — so it only surfaces one self-extinguishing
    # ``[audit]`` finding for the LLM to judge. Default-off for opt-in lived-data
    # validation per the audit-rule convention; zero SDK cost (one bounded git
    # subprocess per distinct cited SHA, memoized within the scan).
    undercredited_landing_enabled: bool = False

    # Reconcile-near-miss audit rule (rule 21) — the pre-landing complement of
    # rule 20 (undercredited-landing). Where rule 20 credits a landing whose
    # lineage SHA git-proves it landed, this flags a ``partial`` remediation on a
    # git-unlanded own PR measured ≤ ``reconcile_near_max_behind`` behind
    # ``origin/main`` whose lineage authored no ancestor-of-main reconcile SHA —
    # the actuator gap in the ≤N-behind band that keeps "awaiting review/merge"
    # structurally available for exactly the case that is mine to land (a
    # cleanly-reconcilable own PR a few commits behind main). Only a new SHA on
    # origin/main extinguishes it; it routes one self-extinguishing ``[audit]``
    # finding into a ``Reconcile+push #N or gh pr close #N`` directive (under
    # ``[loop] owed_act_binding_enabled`` the close clause is dropped while the
    # PR reads ``CODE_OWED``). Default-
    # off for opt-in lived-data validation per the audit-rule convention; zero
    # SDK cost (one bounded merge-truth read + one commits-behind measurement per
    # distinct cited PR, memoized within the scan).
    reconcile_near_miss_enabled: bool = False
    # The near-band ceiling (inclusive) in commits behind ``origin/main`` for
    # rule 21: a PR genuinely behind (>= 1) but within this distance is in the
    # reconcile lane; a greater distance is the disposal band's territory.
    reconcile_near_max_behind: int = 10

    # Green-ready-handoff audit rule (rule 22) — the earliest, most-owned handoff
    # trigger. Every prior handoff arm fires late (the escalation bridge after
    # lap-budget exhaustion; rule 14 on a verdict/SHA
    # contradiction). This fires the instant an owned, git-unlanded PR reads
    # merge-ready — every CI check SUCCESS, no code-clearable merge blocker,
    # waiting only on Thomas's merge — and carries no ``pr_number``-keyed handoff
    # standdown: it routes that PR through the existing ``deliver_handoff``
    # actuator exactly once (one merge-or-close message + the durable standdown).
    # The standdown self-extinguishes the rule and auto-releases on merge/close via
    # the same forced-arm path rule 14 uses — no new capability, no new push.
    # Default-on: the veto-proof handoff lever this rule mechanizes has fired zero
    # times by intention, so a default-off flag would bind nothing; zero SDK cost.
    green_ready_handoff_enabled: bool = True

    # Banked-owned-act-reperformance audit rule — the retrospective artifact-id-axis
    # complement to the prospective ``check_untouched_first_target`` task gate. It
    # reads the three owned-act ledgers (stand-down verdicts, obsolete-prune plans,
    # delivered handoffs) under the data dir and flags a ``next_task_directive`` or
    # queued task goal that re-performs an act already banked there — re-doing it
    # only re-banks a duplicate. Self-extinguishes the moment no directive/goal
    # names a banked act; git-free, zero SDK cost (bounded ledger reads only).
    # Default-off for opt-in lived-data validation per the audit-rule convention.
    banked_owned_act_enabled: bool = False

    # Owned-act-drought audit rule (rule 18) — a self-releasing consequence for
    # a zero-count of chosen terminal acts. When the count of owned terminal
    # acts (``mark_plan_obsolete`` ``obsolete`` plan-status events plus CHOSEN
    # stand-down verdict rows) over the last ``owned_act_drought_window`` cycles
    # is zero AND at least ``owned_act_drought_min_backlog`` prunable
    # ``pending_review`` plans exist, the rule emits one ``[audit]`` finding that
    # routes the owned-act demand into ``next_task_directive`` via the existing
    # audit→directive bridge. Self-extinguishes the instant one owned act of
    # either kind lands in the window. Default-on — a default-off flag would bind
    # nothing; zero SDK cost (bounded reads of plan + stand-down + task records).
    owned_act_drought_enabled: bool = True
    owned_act_drought_window: int = 5
    owned_act_drought_min_backlog: int = 2

    # Branch-promotion-pressure master switch (proposal 116, step 6). Default-on
    # preserves today's behaviour. When false, the scattered sites whose
    # only job is to manufacture pressure to promote a kept task branch into main
    # via a source_task_id follow-up plan go silent at once: audit rule 7
    # (kept_task_not_promoted), the two task-gate directive bridges (promotable
    # backlog / integration cadence), the three task-generation promotion hints,
    # the kept_task_branches_v1 seed
    # limitation mirror, and the step-5e merge-clean auto-direction. Zero SDK cost.
    branch_promotion_pressure_enabled: bool = True

    # Reality-refutation display (proposal 088 Phase 2) — the eyes-win-a-dispute
    # mirror. When true, ``contemplate`` injects a ``REALITY-REFUTATION`` line
    # naming each git-grounded audit finding (phantom resolution,
    # unconfirmed-own-landing) the LLM has refuted across
    # ``audit_reality_refutation_threshold`` consecutive cycles while git still
    # contradicts the claim. Pure display, no behavior change, enableable
    # independently of the actuator; zero SDK cost.
    audit_reality_refutation_display_enabled: bool = False
    audit_reality_refutation_threshold: int = 3

    # Measurement-costume display (proposal 112) — the reflective surface over the
    # frame↔discharge owner. When true, ``contemplate`` injects a
    # ``MEASUREMENT-COSTUME`` line naming the recent tasks whose investigation-
    # framed goal was discharged as tracked construction (``assess_frame_discharge``
    # returned ``COSTUME`` over the task-history window). Self-extinguishing (empty
    # when no task is a costume); pure display, no behavior change, zero SDK cost,
    # default-off. Independent of the record-time cap's ``frame_discharge_bind_enabled``.
    audit_costume_display_enabled: bool = False

    # Undefined-coinage display (proposal 101) — the mint-vs-define feedback
    # loop. When true, ``contemplate`` injects an ``UNDEFINED-COINAGE`` line
    # naming the hyphenated coinages the recent journal is actively minting that
    # ``docs/lexicon.md`` does not yet define. Self-extinguishing (a term drops
    # off once it is defined or falls out of use); pure display, no behavior
    # change, zero SDK cost, default-off.
    audit_undefined_coinage_display_enabled: bool = False

    # Flag-horizon display (proposal 117) — the rollout-cadence forcing function's
    # push-side surface. When true, ``contemplate`` injects a ``FLAG-HORIZON`` line
    # naming each ``EXPERIMENTAL_FLAGS`` entry within (or past) its ``review_by``
    # horizon, marking the already-past ones ``OVERDUE`` — so an approaching
    # promote-or-delete deadline reaches the agent on its own cadence rather than
    # only as a late CI red or a pull-only ``maestro audit-flags`` read.
    # Self-extinguishing (empty when no flag is near or past its horizon); pure
    # display, no behavior change, zero SDK cost, default-off.
    audit_flag_horizon_display_enabled: bool = False

    # Split-due sensor (proposal 147) — when true, ``contemplate`` injects a
    # ``SPLIT-DUE`` line naming the worst source files at or above the split-due
    # line, so a growing module is named before it reaches the size wall.
    # Self-extinguishing, read-only, zero SDK cost, default-off.
    audit_split_due_sensor_enabled: bool = False

    # Split-due directive bridge (proposal 147) — when true, ``audit_self_model``
    # fills an *empty* ``next_task_directive`` with a concern-split directive for
    # the worst source file at or above the split-due line, naming its two
    # largest definitions. Self-extinguishing, zero SDK cost, default-off.
    audit_split_due_directive_enabled: bool = False

    # Undefined-coinage directive bridge (proposal 101) — the actuator half of the
    # display sensor above. When true, ``_audit_self_model`` converts the top
    # undefined coinage the recent journal is actively minting into a one-shot
    # ``next_task_directive`` steering the next task toward defining that term in
    # ``docs/lexicon.md`` (or letting it lapse). Fills only an *empty* slot,
    # defers to owned WIP, and self-extinguishes as terms are defined or fall out
    # of use. Zero SDK cost, default-off.
    audit_undefined_coinage_directive_enabled: bool = False

    # Reality-refutation actuator (proposal 088 Phase 2) — the eyes win a
    # sustained dispute. When true, after a git-grounded finding (phantom
    # remediation/resolution, unconfirmed-own-landing) for a ``#NNNN`` recurs
    # ``audit_reality_refutation_threshold`` consecutive cycles while a matching
    # ``[audit refuted]`` entry disputes it and git still shows the PR unmerged,
    # ``_audit_self_model`` delivers exactly one merge-or-close message plus a
    # durable ``pr_number``-keyed standdown via the escalation bridge. Idempotent
    # per target; auto-released when git shows the PR merged/closed. Default-off;
    # zero SDK cost (one message + ledger write through the existing bridge).
    audit_reality_refutation_enabled: bool = False

    # Retire-unlanded-limitations fuel-starve (proposal 116) — before mirroring a
    # ``limitations_known`` entry into a ``[limitation]`` open question,
    # ``contemplate`` measures how much unlanded plan debt the limitation's
    # question-lineage has already accumulated: tasks whose ``linked_question``
    # falls in that lineage, and the ``obsolete``/``rejected`` evolution plans
    # citing those tasks via ``source_task_id``. A limitation at/over
    # ``generative_debt_threshold`` such plans is not re-surfaced, so it stops
    # anchoring new self-directed work. Self-extinguishing (debt is recomputed
    # from git-and-ledger truth each cycle, so a limitation resurfaces if its
    # lineage's plans start landing); zero SDK cost (pure JSONL reads). Named
    # unprefixed like ``owned_act_drought_*``; default on since the proposal-116
    # graduation (off restores mirroring every limitation).
    retire_unlanded_limitations_enabled: bool = True
    generative_debt_threshold: int = 3

    # Self-calibration input capture (proposal 129) — closes the *input* half of
    # the metacognitive loop. When true, the priced reflection must state its loss
    # condition in ``maestro/self_calibration.py``'s closed vocabulary
    # (``LOSS_CONDITION_GRAMMAR``) and attach a numeric ``CONFIDENCE:``, and each
    # prediction row gains two additive fields: the stated confidence and the
    # ``ProbeKind`` its loss condition parsed to (or nothing when the grammar
    # declines — the unfalsifiable signal). No bet is scored here and the
    # ``PRIOR VERDICT:`` self-grade path is untouched — this only records the
    # measurement scoring will later need. When false the reflection prompt and
    # the ledger rows are byte-identical to today's. Zero SDK cost (parse only),
    # default-off.
    audit_self_calibration_enabled: bool = False

    # Calibration verdict authority (proposal 129) — closes the *output* half of
    # the metacognitive loop. When true, ``agent.contemplate`` scores each open
    # reflection bet through ``self_calibration.score_loss_condition`` and lets
    # ``author_reflection_verdict`` — not the model's ``PRIOR VERDICT:`` self-grade
    # — author the ledger verdict, retaining the self-grade beside it as narration
    # and stamping every measured-vs-narrated divergence to the gate-activation
    # trace. A bet whose window has not matured records ``pending`` and is
    # re-scored on later cycles; a bet the grammar cannot express keeps the narrated
    # verdict. Independent of ``audit_self_calibration_enabled`` (grammar capture):
    # with capture off every bet reads unfalsifiable and this degrades to the
    # narrated path. Zero SDK cost (ledger reads only); off ⇒ rows byte-identical to
    # today's. Default-off.
    audit_calibration_verdict_authority_enabled: bool = False

    # Evidence-bound self-knowledge (proposal 129) — makes each
    # ``capabilities_known`` claim carry a machine-checkable citation to something
    # that already happened. When true, ``update_self_model`` asks for a
    # ``capability_evidence`` object mapping each new/revised claim to one compact
    # citation (a successful task id, an archived merge SHA, or an emitted
    # behavioural-sensor tag), resolves it deterministically at write time
    # (existence + polarity, never entailment) and records it in
    # ``SelfModel.claim_evidence``. Consuming that signal is a follow-on change.
    # Zero SDK cost beyond the prompt segment; off ⇒ no response key is read, no map
    # entry is written, and the update prompt is byte-identical to today's.
    # Default-off.
    audit_evidence_bound_claims_enabled: bool = False

    # Rule 23 — closed-reference claim detector: flags a self-model entry whose
    # every cited ``#NNN`` the code host reports closed. Bounded read-only
    # ``gh api`` lookups in the audit phase; zero SDK cost. Default-off.
    audit_closed_reference_enabled: bool = False

    # Refutation accountability (proposal 129 Phase C) — the eyes win a dispute
    # across the *whole* mechanical-fact audit class, and the correction owed is
    # to the self-model, not a message queue. When true, ``_audit_self_model``
    # runs ``audit_checks.measure_refutation_rejections`` after persisting the
    # cycle: a refuted ``[audit refuted]`` open question whose rule is classed
    # ``mechanical_fact`` in ``RULE_AUTHORITY`` (a git-ancestry read, a
    # plan-store transition, or the message/plan ledger) and that keeps
    # re-firing on the same target for ``audit_refutation_rejection_cycles``
    # consecutive cycles is mechanically restored to its canonical ``[audit]``
    # form, with the override stamped in the gate trace, the journal, and the
    # existing ``REALITY-REFUTATION`` contemplation line. Derived measures and
    # heuristics stay refutable indefinitely. Self-extinguishing (a finding that
    # stops re-firing is never restored; the restored question is re-refutable
    # and only overturned again while the evidence persists). Zero SDK cost
    # (pure ledger reads plus one self-model rebind); default-off ⇒
    # ``open_questions`` and the gate trace are byte-identical to today's.
    audit_refutation_accountability_enabled: bool = False
    # Consecutive re-firing cycles a refuted mechanical-fact finding must reach
    # before its refutation is mechanically overturned (``int >= 1``); mirrors
    # ``audit_reality_refutation_threshold``'s validation shape.
    audit_refutation_rejection_cycles: int = 3

    # Version archive — snapshots metrics per merged evolution PR.
    # ``version_archive_enabled=false`` turns ``_snapshot_version`` into a
    # no-op for deployments that do not want the feature.  The window size
    # controls how many recent tasks feed the rolling metrics.
    version_archive_enabled: bool = True
    version_archive_window_iterations: int = 10

    # Evolution PR tending — runs every ``tend_prs_every`` iterations (and
    # always on the first iteration after restart when enabled).  When
    # ``tend_prs_enabled`` is false the phase is skipped entirely.  The
    # cooldowns prevent the (purely mechanical) tender from re-routing
    # the same PR every iteration after a conflict or broken-gate
    # detection.
    tend_prs_enabled: bool = True
    tend_prs_every: int = 1
    conflict_resolution_cooldown_hours: float = 24.0
    quality_fix_cooldown_hours: float = 24.0
    # Proposal 137: the first rung of the bounded backoff both cooldowns decay
    # through while their observation signature stays unchanged.  The window is
    # ``min(base * 2 ** attempts, <the cooldown's own hours ceiling>)``, so a
    # base at or above the ceiling simply degrades to today's flat window.
    # ``attempts`` counts only the laps that genuinely bounced off the cooldown
    # — a bypassed, pushing or unmeasurable lap leaves the rung unconsumed.
    # Only read when ``cooldown_signature_keying_enabled`` is on.
    cooldown_backoff_base_hours: float = 1.0

    # Proposal 137: when true, both tend cooldowns are keyed to the signature
    # the anchor was stamped against — a provably-changed component releases
    # the cooldown before the hours window is consulted, so a verdict about a
    # head that no longer exists cannot outlive it.  The conflict cooldown keys
    # on the full ``(branch head, origin/main base, CI rollup conclusion)``
    # triple (generalising its proposal-099 base release); the quality-fix one
    # keys on ``(branch head, CI rollup conclusion)``, since an ``origin/main``
    # advance says nothing about whether this PR's own red changed.  Off, both
    # predicates are the pure wall-clock windows (including
    # ``_in_conflict_cooldown``'s own base-SHA comparison), byte-identical to
    # today.
    cooldown_signature_keying_enabled: bool = False

    # Proposal 137: when true, the owed act an open own PR presents (the
    # ``pr_owed_act`` oracle's verdict, derived from a readiness fact read
    # through the per-iteration-memoized ``observe_pr_readiness`` chokepoint —
    # at most one observation per cited PR) binds the two give-up decisions that
    # still run on weaker facts.  Audit rule 21 drops the ``or gh pr close #N``
    # clause from its finding while the PR is ``CODE_OWED`` — a failing gate the
    # loop can fix itself is not a co-equal reason to close — and
    # ``assess_pr_salvage`` holds its disposal veto on code-owed work that is
    # still inside ``RECONCILE_COMMITS_BEHIND_BUDGET``.  Off, rule 21's finding
    # text and the salvage verdict table are byte-identical to today.
    owed_act_binding_enabled: bool = False

    # When true, the tend phase scans each open evolution PR for unresolved
    # AI *bug* review comments (the 🐛 severity ``maestro-review`` fails the
    # build on) and routes a remediation task — even when the PR is otherwise
    # mergeable.  Mirrors the human-paced comment-remediation workflow inside the loop.
    tend_bug_comments_enabled: bool = True

    # When true, an open evolution PR stays in the tend set even after its plan
    # record leaves ``implemented`` (retired to ``obsolete`` by a self-prune or
    # the orphaned-cure sweep while the PR is still open on GitHub).  Without
    # this the tend filter would drop the PR the moment the status changed,
    # leaving a zombie PR nobody freshens, reminds on, or routes comments for.
    # Set false to restore the strict implemented-only worklist.
    tend_planless_prs_enabled: bool = True

    # Gates the tend phase's ``origin/main`` merge-and-push in
    # ``_tend_single_pr`` — the reversible kill-switch for the merge-queue
    # cutover (proposal 128). Default on preserves current behaviour
    # byte-for-byte; flip to ``false`` alongside enabling GitHub's merge queue
    # on ``main`` so the loop stops eagerly re-syncing behind PRs (a merge queue
    # tests each PR once, at the front, against the state it lands into). With
    # the flag off the tend phase keeps every other role (quiet-PR reminders,
    # conflict routing, quality-gate routing, delivery of local remediation
    # commits) and only skips the redundant main-resync.
    tend_main_merge_enabled: bool = True

    # When true, a remediation lap that confirmed both deliveries (each terminal
    # reply posted without error AND the fix pushed to ``origin/<pr_branch>``)
    # resolves the addressed AI-finding review threads on GitHub (proposal 091).
    # The loop is already the sole trusted reply-poster and pusher; it is the
    # only resolver too.  Only threads rooted in an AI finding (🐛/💡/✏️) whose
    # reply this lap delivered and that carry no marker-less (human) reply are
    # resolved — human participation is never auto-resolved.  Set false to leave
    # thread resolution to the human (the pre-091 behavior).
    resolve_addressed_threads_enabled: bool = True

    # When true, the task-execution action gate strips shell redirections
    # (``2>&1``, ``> out``, ``&>``, …) from a segment's tokens before it reads a
    # ``git push`` refspec, so an output-captured own-branch push
    # (``git push origin maestro/evolve/<id> 2>&1 | tail -12``) is no longer
    # mis-parsed — the leaked ``2``/``1`` file-descriptor tokens read as stray
    # refspecs and fail-close the instructed push (proposal 096).  Default on:
    # the shipping gate strips redirections so its verdict finally matches the
    # push the loop instructs.  Stripping only ever converts a fail-*closed* deny
    # of a genuinely-owned, non-force push into an allow — ``main``/``master``,
    # force-pushes, out-of-set branches, and unparseable pushes all stay denied.
    # Rollback: flip back to ``false`` to restore the pre-096 tokenizer.
    gate_strip_redirections_enabled: bool = True

    # When true, an unconsumed ``pending_user_requests`` entry that names a PR
    # (``#NNNN`` / ``PR NNNN``) is an absolute veto on authoring *either* kind of
    # stand-down verdict for that PR — the chosen (LLM-narrated ``[stood-down]``)
    # write and the mechanical actuator alike — independent of thread state
    # (proposal 091). A human message explicitly naming a PR is the strongest
    # "this is not settled" signal there is; a stand-down authored while it sits
    # queued would silently mute it. Default-on because the flag only ever
    # *refuses* a suppression, never authors one; set false to restore the
    # thread-only behaviour if the PR-reference match ever misfires.
    user_request_blocks_standdown_enabled: bool = True

    # When true, a drained ``pending_user_requests`` message that names one of
    # Maestro's own open ``maestro/evolve/*`` PRs is routed as a full
    # PR-remediation task (PR branch checked out, reply manifest, direct push)
    # instead of a generic one, and — before the task runs — that PR's
    # stand-down verdicts and escalation-ledger standdown are released so a
    # follow-up tend lap cannot instantly re-mute the human demand (proposal
    # 091).  A message naming a settled/foreign PR, or no PR at all, falls
    # through to today's generic path unchanged.  Set false to restore the
    # pre-091 behavior where every user message runs as a generic task.
    user_pr_message_remediation_enabled: bool = True

    # When true, the tend phase sends Thomas a Telegram "ready for re-review"
    # ping on a post-delivery sweep that confirms the delivered head's CI
    # rollup is green/none with no outstanding review comments (deferred from
    # the push pass, where CI on the fresh merge commit is still pending).
    # Fires at most once per delivered head.  Requires
    # ``tend_bug_comments_enabled``: the "no comment unaddressed" claim is only
    # meaningful when comment-tending is the source of truth, so the ping never
    # fires when that flag is off.
    pr_ready_for_review_notify_enabled: bool = True

    # Remediation repetition gate — after this many consecutive no-op
    # remediations of the same PR, ``_tend_single_pr`` stops re-routing
    # until the branch receives new commits.  Experimental.
    remediation_noop_threshold: int = 2
    remediation_noop_gate_enabled: bool = True

    # Costume-signature dequeue gate — when a PR dequeued for remediation has
    # already recorded ``branch_resolved_repetition_threshold`` or more
    # ``partial`` laps whose summary carries the "awaiting tend push" marker
    # (the agent's documented "resolved on branch" costume), zero-cost-skip the
    # next lap as a ``partial`` no-op and escalate once to Thomas/Tend rather
    # than re-lapping the wheel. Ordered immediately after the merge-block gate.
    branch_resolved_gate_enabled: bool = True
    branch_resolved_repetition_threshold: int = 2

    # Conflict-aware merge-block classification. When the merge-block gate
    # resolves a PR to ``BEHIND_MAIN`` (Tend PR catches the branch up to
    # ``origin/main`` mechanically), probe the merge *locally* — an in-memory
    # ``git merge-tree`` of the branch against ``origin/main`` — to decide
    # whether catching up actually merges cleanly. A behind-AND-conflicting
    # branch is reclassified ``CONFLICTING`` (only the LLM can resolve it) so it
    # is routed to real remediation instead of skipped as Tend-clearable. The
    # local probe is deterministic and immune to GitHub's flapping
    # ``mergeable``/``merge_state_status`` recomputation lag. Default on; flag
    # disables it if the extra git op is unwanted.
    merge_block_local_probe_enabled: bool = True

    # PR ledger — maximum remediation-attempt budget per origin-head SHA.
    # Once exhausted at a given head the PR is latched stuck; head movement
    # (Tend pushed something) resets the budget automatically.
    pr_remediation_max_attempts_per_head: int = 3

    # Per-PR lap budget across all heads (proposal 080 Phase 3). Counts a PR's
    # cumulative remediation laps that landed nothing per the host-API merge
    # oracle (``measure_unlanded_remediations``); unlike the per-head ledger
    # above, head movement does NOT reset it. When the count reaches the budget,
    # Tend delivers exactly one merge-or-close handoff to Thomas, writes a
    # durable ``pr_number``-keyed standdown, and suppresses further remediation
    # minting for that PR until the host API reports it merged/closed. ``0``
    # disables the lap-budget standdown entirely.
    pr_remediation_lap_budget: int = 6

    # Freshness TTL for the ``MergeTruth`` chokepoint (proposal 087 step 4).
    # ``MaestroAgent.is_pr_settled`` reuses a memoized observation only while it
    # is younger than this many seconds; past the TTL it re-observes (re-fetches)
    # rather than letting a stale fact author a verdict. ``0`` forces a fresh
    # observation every call. Within the TTL the memo is reused and no fetch is
    # re-invoked. Conservative default — bounds re-fetch cost while keeping
    # within-iteration staleness no worse than today's per-iteration memo.
    merge_truth_ttl_seconds: int = 300

    # Pre-stamp merge-truth gate (proposal 087 step 5). When true, a remediation
    # ``success`` outcome naming ``#N`` whose ``MaestroAgent.is_pr_settled``
    # reports unmerged is downgraded to ``partial`` at write time, stamped with
    # the observation's provenance (``source`` / ``observed_at``) — moving the
    # catch that audit rule 14 makes
    # post-hoc to pre-stamp, so the false success never becomes load-bearing.
    # Default off: today's host-probe stamp gate runs unchanged until flipped.
    prestamp_merge_truth_gate_enabled: bool = False

    # Observed ready-and-waiting stand-down gate (proposal 089 step 3). When
    # true, the dequeue chain consults a live
    # ``MaestroAgent.observe_pr_readiness`` fact and, for an open PR whose every
    # code-clearable blocker is clear (the sole remaining gate being Thomas's
    # approval or Tend's push), records a zero-cost ``partial`` ``[ready]`` and
    # evicts the request — the work is done, the PR is in Thomas's court.
    # Recomputed live each pop, so a blocker reappearing on the same head
    # re-admits the PR immediately. Default on (proposal endorsed by review):
    # ends the ready-and-waiting re-service treadmill without suppressing
    # self-clearable work, since ``ready_and_waiting`` already excludes
    # conflict/behind/threads/overflow. With the flag off the gate is a no-op
    # and the dequeue chain is byte-identical to the pre-089 chain.
    observed_ready_standdown_enabled: bool = True

    # CI-pending remediation-slot yield gate. When true, ``_run_task_phase``
    # consults ``MaestroAgent._gate_pr_remediation_ci_pending_hold`` before it
    # pops a queued PR-remediation entry: for an owned open PR whose sole live
    # blocker is CI still running (no merge conflict, no failing quality gate, no
    # unaddressed AI-finding thread) there is nothing to fix, so the slot is
    # yielded with the PR left queued (no eviction), no SDK session opened, and no
    # stand-down / escalation / close — CI concludes on its own clock rather than
    # being reset by a reconcile-push lap. Distinct from the observed-ready
    # eviction gate (which keys on CI *green* and evicts) and the merge-block /
    # stand-down handoffs. Default on: with the flag off the gate is a full no-op
    # and the selection chain is byte-identical to today.
    ci_pending_hold_dequeue_enabled: bool = True

    # One-shot green-handoff arm on the CI-pending hold (#3630). When true, at the
    # exact point the hold gate above has already established "CI still running is
    # the sole live blocker" it runs the same demanding local gate pair (ruff →
    # mypy) ``red_root.diagnose_red_root`` owns against the head, and on
    # ``all_passed`` delivers the merge-or-close handoff through the shipped
    # ``deliver_handoff`` actuator and its ``pr_number``-keyed stand-down — so a
    # locally-green, CI-pending PR becomes one durable owned act instead of a
    # silent re-yield. Self-extinguishing: the ``has_standdown`` read is evaluated
    # *first*, so a PR already handed up never pays for the gate subprocess, and
    # the arm fires at most once per PR. The gate's return value is unchanged in
    # every branch — the slot is still yielded and the PR still left queued, with
    # no push, merge, or close capability added. Default off: with the flag off the
    # hold gate is byte-identical to today.
    ci_pending_green_handoff_enabled: bool = False

    # Salvage-before-disengage master flag (proposal 094 step 3). When true,
    # both stand-down writers — the chosen (LLM-narrated ``[stood-down]``)
    # verdict in ``_record_remediation_outcome`` and the mechanical actuator in
    # ``_dispose_mechanical_standdown`` — first consult the pure
    # ``maestro.pr_facts.assess_pr_salvage`` oracle: while an open own PR
    # carries a code-clearable blocker (``PRReadiness.ready_and_waiting`` is
    # false) and its work is not disposable, neither writer may stand it down,
    # so the PR stays eligible for the next servicing lap. Disengagement stays
    # reachable for a done-and-waiting or genuinely disposable PR. Also activates
    # the progress-exhaustion-gated merge-or-close handoff via
    # ``progress_aware_lap_budget_active`` (both handoff sites and the per-lap
    # readiness snapshot), so flipping this on lights the progress-aware handoff
    # up at every handoff site in the same step. Default on: set it false to
    # restore the prior give-up behavior, where the oracle is consulted by nobody
    # and both writers stand down exactly as before.
    salvage_before_disengage_enabled: bool = True

    # Work-in-progress primacy gate (proposal 092 step 1). When true, a
    # highest-precedence ``wip_primacy`` block gate at the head of ``GATE_CHAIN``
    # (and the audit→directive bridge) stands down discretionary task generation
    # while an open own ``maestro/evolve/*`` PR carries a merge blocker the loop
    # can clear itself (``CONFLICTING`` / ``THREADS_UNRESOLVED``): finish started
    # work before minting new. Default on — the owner is consulted every
    # iteration so finishing owned work outranks new discretionary generation.
    wip_primacy_enabled: bool = True

    # Refutable stand-down (proposal 135). When true, ``route_feedback``'s tend
    # upkeep runs a third standdown release path beside the merge/close and
    # expiry pruners: ``prune_refuted_standdowns`` re-asks the question a
    # ``handoff-ready`` latch answered, against the owed act the tend walk just
    # measured, and drops every record the live reading refutes — so a handed-off
    # PR a sibling merge turned CONFLICTING stops suppressing the remediation
    # that would clear it. The walk's ``owed_acts_observed`` population is *not*
    # gated by this flag (proposal 137) — it is measured on every lap in every
    # environment, and the release reads it instead of making any host call.
    # Default off: the release path is inert and today's latch behaviour is
    # byte-identical until this is deliberately switched on.
    refutable_standdown_enabled: bool = False

    # Refuted-release budget (proposal 135). Bounds how many times one PR's
    # ``handoff-ready`` latch may be released by refutation before the latch
    # becomes permanent. Without a bound a PR that flaps between merge-ready and
    # CONFLICTING would cycle handoff -> refutation -> handoff indefinitely,
    # trading a silent deadlock for an unbounded stream of handoff and
    # withdrawal messages. Once this many releases are on record
    # ``prune_refuted_standdowns`` stops releasing, leaves the record in place,
    # and says so once in a terminal message, so the PR surfaces as a bounded,
    # named failure rather than a treadmill. Only read on the
    # ``refutable_standdown_enabled`` path.
    standdown_refutation_release_cap: int = 2

    # Owned-act preemption of the stalled-remediation yield slot. When true, a
    # stalled ``pending_pr_requests`` head that would yield its slot to the next
    # non-stalled remediation entry (#2837) is instead redirected to the owned
    # mechanical prune path (``sweep_clustered_pending_plans`` /
    # ``sweep_restatement_pending_plans``) *iff* the rule-18 owned-act-drought
    # predicate is active (zero owned terminal acts in-window AND a prunable
    # ``pending_review`` backlog). The measured contradiction — I re-lap an
    # externally-addressed PR while my owned prune never wins a slot — is refused
    # by moving net ``pending_review`` now instead of counting the fire. Reuses
    # the existing drought predicate (self-extinguishes the instant one owned act
    # lands in the window) and the existing zero-SDK sweep. Default off /
    # experimental: with the flag off the selection chain is byte-identical to
    # today, so a healthy PR behind the stalled head takes the yielded slot
    # exactly as before.
    owned_act_preempt_enabled: bool = False

    # Proposal 118: chain-emission arm of multi-PR evolution proposals. When
    # true, ``evolve()`` routes reflection through
    # ``EvolutionEngine.propose_plan_chain``: an intent too large for one PR is
    # decomposed into an ordered chain of thin slice plans owned by one
    # ``EvolutionProposal``, and only the head (``phase_index == 0``) slice is
    # submitted for the usual per-slice human review while the rest persist
    # dormant (``"sliced"`` status). A single-PR intent (or a chain of fewer
    # than two valid slices) collapses to today's lone-plan path. Default off /
    # experimental: with the flag off ``evolve()`` calls ``propose_plan`` →
    # ``submit_for_review`` exactly as before, so the loop is byte-identical.
    multi_pr_proposals_enabled: bool = False

    # Proposal 099: the conflict-remediation give-up conditions re-key on base
    # advance. The three mechanisms that decide "stop trying" on an own
    # conflicting PR — the stalled-lineage stall predicate
    # (``_is_stalled_remediation_lineage``), the tend conflict cooldown
    # (``_in_conflict_cooldown``), and the salvage disengage oracle
    # (``assess_pr_salvage``) — break on a moved ``origin/main`` exactly as they
    # already break on a moved PR head: a head that did not advance against a base
    # that *did* move is a lost race that has never been re-run, not unchanged
    # externally-blocked work. Repeated failure against the *same* base still stops
    # at the same cap. ``_audit_self_model`` also escalates a losing race visibly —
    # one durable, idempotent heads-up to Thomas for a lineage that re-entered on
    # base advance at or beyond this threshold without ever landing, recording
    # **no** stand-down so the PR stays fully tend-eligible. The per-PR re-entry
    # count at or above which that heads-up fires (``int >= 1``); mirrors
    # ``audit_reality_refutation_threshold``'s validation shape.
    unwinnable_race_re_entry_threshold: int = 3

    # Proposal 108: make the CLAUDE.md-maintenance mandate opt-in. When true, the
    # evolution prompts (propose_plan / implement / self_review) and the
    # planner's doc-update nudge carry the legacy hard mandate ("Every evolution
    # MUST update CLAUDE.md"). When false (the default) they carry the soft,
    # conditional guidance already codified in the Evolution Protocol ("update
    # CLAUDE.md only if an invariant changed"). The loop never reads CLAUDE.md at
    # runtime, so the mandate only manufactured cap-vs-mandate churn; flipping this
    # one boolean restores today's wording verbatim.
    require_claude_md_update: bool = False

    # Post-session mechanical landing actuator. When true, ``run()`` invokes the
    # ``session_landing`` actuator after a PR-remediation session and before
    # verdict composition: gate-clean working-tree edits on the owned
    # ``maestro/{task,evolve}/*`` branch are mechanically committed and pushed
    # (owned+oracle-gated), rewound on a RED gate result, so the head SHA moves
    # by the loop's hand rather than a narrated "delivered". The composed verdict
    # is then grounded on the post-push ``is_pr_settled`` read. Default off — the
    # legacy commit-only-then-§5-push path is unchanged when disabled.
    session_landing_enabled: bool = False

    # Obligation-fork verdict floor wiring (PR #3211). When true, ``run()``'s
    # §4c remediation action-zero records an obligation-fork marker
    # (``record_obligation_fork_truth`` — ``git rev-parse HEAD`` vs
    # ``origin/<pr_branch>``) alongside the fetch-truth marker, and §4e passes
    # ``owned_awaiting_push`` to :func:`gate_remediation_verdict` — derived from
    # :func:`classify_blocker_ownership` on this lap's recorded merge-block
    # (``AWAITING_PUSH`` is owned) — so the obligation-fork floor's
    # ``[fork-missing]`` / ``[push-owed]`` / fetch-gated pass-through arms
    # actually fire on an owned unpushed head instead of remaining dead code.
    # Default off: the gate is called without ``owned_awaiting_push`` (the
    # byte-identical pre-wiring path) and no fork marker is written, so the
    # floor stays inert until the pending push actuators co-activate it.
    obligation_fork_floor_enabled: bool = False

    # Committed-but-unpushed push actuator. When true, ``_record_remediation_outcome``
    # mechanically fires the owed ``git push`` on a gate-clean *committed* owned
    # head that is ahead of ``origin/<pr_branch>`` under an ``AWAITING_PUSH``
    # blocker — the committed-work sibling of the always-on uncommitted-edit arm.
    # Green-gated (a RED quality gate rewinds the local head to origin instead of
    # pushing, so a red head never reaches origin), oracle-gated, delivery bound
    # to a same-turn ``verify_push_advanced_remote_head`` read (not the push
    # return), and self-extinguishing once the remote head catches up. Default
    # on — flipping this off restores the legacy path (the push surfaces only
    # as an OWNED_PUSH_AVAILABLE marker) byte-identical to the pre-promotion
    # behavior.
    owned_committed_push_actuator_enabled: bool = True

    # Progress-aware lap budget (proposal 089 step 5). When true, the
    # merge-or-close handoff (``escalation_bridge.detect_unlanded_cure_handoff``)
    # triggers on consecutive *no-progress* laps
    # (``measure_no_progress_laps >= pr_remediation_lap_budget``) instead of the
    # gross unlanded-lap count — so a PR clearing a freshly-arrived blocker every
    # lap (the healthy churn of a fast main) never escalates while a genuinely
    # stuck one still does. Default off: the gross-count trigger is unchanged.
    progress_aware_lap_budget_enabled: bool = False

    # Dropped fire-or-close directive replay (proposal 090). When true, a
    # write-time fire-or-close ``next_task_directive`` that loses the one-shot
    # slot race is appended to the ``dropped_directives.jsonl`` ledger together
    # with its git grounding (cited PR numbers / own-branch commit SHAs); the
    # lowest-precedence ``dropped_directive_replay`` task gate re-offers the
    # oldest still-live entry into a later empty slot and prunes entries whose PR
    # has settled or whose SHA has landed. Default off: with the flag off the
    # writers never append and the gate never fires, so the gate chain and the
    # record-time binds are byte-identical to today.
    dropped_directive_replay_enabled: bool = False

    # Self-regulating burn-rate throttle. When the rolling-window spend rate
    # reaches ``max_loop_budget_usd_per_hour``, the inter-iteration sleep is
    # stretched by a multiplier proportional to ``rate / limit`` (capped at
    # ``budget_throttle_max_multiplier``). All phases keep running — only the
    # gap between iterations stretches. As samples age out of the rolling
    # window, the rate falls, the multiplier falls, and the loop naturally
    # returns to base pace. ``0`` disables the throttle entirely.
    max_loop_budget_usd_per_hour: float = 0.0
    budget_rate_window_hours: float = 1.0
    budget_rate_min_cost_usd: float = 0.10
    budget_throttle_max_multiplier: float = 60.0

    # Tiered-memory archive rotation. When > 0, dream-cycle archival
    # splits ``task_history.jsonl`` and ``evolution_plans.jsonl`` into a
    # hot file (records newer than ``archive_threshold_days``) and a
    # cold sibling (``<file>.archive.jsonl``). ``0`` disables rotation —
    # both files grow unbounded as before.
    archive_threshold_days: int = 0

    # Rate-limit warning throttle — per-SDK-call sleep applied before the
    # next query() when the sticky multiplier is > 1. ``base_seconds`` is
    # the unit-of-sleep and ``hysteresis`` is the number of consecutive
    # clean query() calls required before the multiplier halves.
    # Higher hysteresis = stickier (safer), lower = more responsive.
    sdk_warning_throttle_base_seconds: int = 10
    sdk_warning_throttle_hysteresis: int = 3
    sdk_warning_hold_threshold: int = 3
    sdk_warning_hold_probe_interval: int = 60
    sdk_warning_hold_min_utilization: float = 0.95

    # Rate-limit extended idle — when in-place rate-limit retries exhaust
    # ``_MAX_CONSECUTIVE_RATE_LIMITS`` the loop no longer crashes.  It
    # instead enters an extended idle state and probes the API with
    # exponential backoff (``base * 2**attempt`` capped at ``max``) using
    # a low-effort single-turn ``query()`` call.  On probe success the
    # counter resets and the loop resumes at the failed phase.
    sdk_rate_limit_idle_probe_interval: int = 300
    sdk_rate_limit_idle_max_probe_interval: int = 3600

    # Startup cooldown when the prior run crashed with a rate-limit SDK
    # error. Gives the upstream rate-limit window time to clear before
    # the new process makes its first SDK call. ``0`` disables.
    startup_rate_limit_cooldown_seconds: int = 60

    # Maximum concurrent SDK queries.  When proposal 007 (parallel steps)
    # lands, ``asyncio.gather()`` will fire multiple ``query()`` calls;
    # this semaphore caps the aggregate concurrency.
    sdk_max_concurrent_queries: int = 2

    # Proposal 139 — route the evolution-lineage ``SINGLE_USE`` callers
    # through ``call_policy.single_use_call`` (one session per call at its
    # registry effort) instead of the shared evolution session. Default-off.
    sdk_single_use_evolution_calls_enabled: bool = False

    # Proposal 140 step 4 — the public SDK seam resolves every call's effort
    # from ``call_policy.tier_effort`` and refuses a session turn whose tier
    # effort disagrees with its session's. Default-off; requires
    # ``sdk_single_use_evolution_calls_enabled`` and
    # ``self_review_own_session_enabled``.
    sdk_call_policy_resolution_enabled: bool = False

    # Proposal 142 — the public SDK seam derives every call's ``tools`` from
    # its caller's declared ``ToolSurface`` and refuses a call (or session
    # turn) the surface does not admit. Default-off.
    sdk_enforced_tool_surface_enabled: bool = False

    # Per-message stream-idle timeout (seconds) for the SDK's SSE stream.
    # When the stream goes silent for this long without yielding the next
    # message, the consumer raises ``SDKError(STREAM_STALL)`` so the
    # loop's resume-in-place retry path can recover.  ``0`` or a negative
    # value disables the safety net.
    sdk_stream_idle_timeout_seconds: float = 180.0

    # Per-message stream-idle timeout (seconds) for *tool-using* callers
    # (``evolution.implement`` / ``self_review`` / ``auto_fix`` and
    # ``executor.execute``).  These callers deliberately skip the
    # ``complete_on`` predicate because ``client.interrupt()`` would drop
    # in-flight tool output, so they rely on the stream-idle safety net
    # alone.  Under ``effort="high"`` with adaptive thinking the model
    # can legitimately sit silent between tool calls (and especially on
    # final wrap-up thinking) for minutes — the short default used by
    # text/JSON callers is too tight here, and a caller that shells out to
    # run the quality gates is legitimately silent for the whole suite.
    # Guarded strictly ``> selfcheck_max_timeout`` in
    # ``_check_cross_field_and_subtable`` so the net always outlasts the
    # largest budget one gate run may spend.  Honours the same ``<= 0``
    # disable semantics as ``sdk_stream_idle_timeout_seconds``.
    sdk_tool_stream_idle_timeout_seconds: float = 1800.0

    # Standby window — pauses all SDK calls during peak hours (UTC).
    # ``start_hour`` is inclusive and ``end_hour`` is exclusive.
    # Wraparound (start > end) is supported for windows that cross
    # midnight UTC.
    standby_enabled: bool = False
    standby_start_hour_utc: int = 13  # 1:00 PM UTC
    standby_end_hour_utc: int = 19    # 7:00 PM UTC

    # Peak-hours scheduled budget throttle. Inside the configured window the
    # burn-rate throttle's effective budget limit is multiplied by
    # ``peak_throttle_rate_multiplier`` (a value < 1.0 tightens it), so the
    # *existing* burn-rate sleep-multiplier fires sooner and the loop slows
    # during peak. Heavy LLM phases are NOT gated off by the window itself —
    # only a genuine burn-rate breach (against the untightened limit) still
    # gates phases, exactly as it does off-peak. This is purely a slowdown,
    # so it requires ``max_loop_budget_usd_per_hour > 0`` to have any effect.
    #
    # ``peak_throttle_timezone`` is an IANA zone name (e.g.
    # ``America/Los_Angeles``); the start/end hours are wall-clock hours in
    # that zone, so DST transitions are handled automatically.
    # ``weekdays_only=True`` restricts firing to Mon-Fri in the same zone.
    # ``peak_throttle_rate_multiplier`` must be > 0; values >= 1.0 loosen the
    # limit (1.0 = no slowdown).
    peak_throttle_enabled: bool = False
    peak_throttle_timezone: str = "America/Los_Angeles"
    peak_throttle_start_hour: int = 5   # 5 AM in peak_throttle_timezone
    peak_throttle_end_hour: int = 11    # 11 AM in peak_throttle_timezone
    peak_throttle_weekdays_only: bool = True
    peak_throttle_rate_multiplier: float = 0.5

    # CLI
    journal_default_count: int = 10
    log_tail_lines: int = 50

    # Telegram
    telegram_max_length: int = 4096

    # Messaging
    message_max_length: int = 2000

    # Email
    email_address: str | None = None
    email_password: str | None = None
    email_thomas: str | None = None
    email_smtp_host: str = "smtp.zoho.com"
    email_smtp_port: int = 587
    email_imap_host: str = "imap.zoho.com"
    email_imap_port: int = 993
    email_max_length: int = 50000

    # Logs
    log_dates_shown: int = 5
    session_data_days: int = 10
    # Application log rotation. ``maestro.log`` is rotated by
    # ``RotatingFileHandler`` past ``log_max_bytes``; ``log_backup_count``
    # backups are kept (``maestro.log.1`` … ``maestro.log.N``). Without
    # rotation the file grows unbounded and the dashboard's tail reader
    # has to scan the entire history on every poll.
    log_max_bytes: int = 50 * 1024 * 1024
    log_backup_count: int = 3
    # When true, ``SessionLogger`` writes a provenance-stamped ``.jsonl``
    # transcript sidecar next to each SDK ``.log`` file (proposal 104).
    # Default-on: real sessions gain attribution out of the box. The raw
    # ``.log`` path is byte-compatible either way — a ``.log`` with no
    # sidecar (legacy, or the flag turned off) still renders as the raw blob.
    structured_transcripts_enabled: bool = True
    # Per-turn content cap for the ``.jsonl`` transcript sidecar (proposal 104).
    # Independent of the human ``.log``'s ``_truncate`` bound: a generous
    # ~40x the 500-char ``.log`` bound, large enough to hold a real reasoning
    # turn while bounding pathological growth. Operator-tunable; bounds every
    # turn now that the sidecar flag defaults on.
    transcript_content_max: int = 20000

    # CI verification
    cov_fail_under: int = 97

    # Executor loop detection — abort step after this many consecutive
    # identical tool-call fingerprints.
    executor_loop_detect_repeats: int = 3

    # Executor empty-response retry — when the SDK returns an empty
    # ``response`` for a step, retry the query this many times before
    # failing. ``0`` disables the retry (first empty response fails the
    # step). Only empty responses are retried — loop-detected failures
    # and ``SDKError`` propagate without retry.
    executor_empty_response_retries: int = 1

    # Evolution self-review. Two roles: an enable gate (``0`` disables
    # self-review entirely) and — under progress-gated gate convergence
    # (proposal 110) — a ceiling-floor input. It is no longer a hard round
    # terminal: ``self_review`` folds it into the convergence ceiling (``max``
    # with ``gate_convergence_ceiling``) and grinds while each round makes
    # progress, exactly as ``auto_fix_max_rounds`` does for the gate loop two
    # fields down. The ``>= 0`` validator logic is unchanged.
    self_review_max_rounds: int = 2

    # When on, ``self_review`` runs each round on its own single-use session
    # at the registry's ``high`` effort (carrying the plan's action-gate
    # hooks) instead of a turn on the implementer's session. Default-off.
    self_review_own_session_enabled: bool = False

    # Quality-gate / auto-fix loop input inside a single ``implement_plan()``
    # call. Under unconditional gate convergence (proposal 110) this is no
    # longer a hard cap: it feeds the convergence ceiling floor (``max`` with
    # ``gate_convergence_ceiling``) rather than terminating the loop. Lives next
    # to ``self_review_max_rounds`` which it mirrors.
    auto_fix_max_rounds: int = 3

    # Gate convergence (proposal 110) — ``implement_plan``'s post-implementation
    # fix loop routes unconditionally through
    # ``EvolutionEngine.converge_to_green``, which keeps grinding while the
    # failing-gate set strictly shrinks (a *failures-shrank* signal, not the
    # *change-made* signal of the retired capped loop) instead of stopping at a
    # fixed round cap. ``gate_convergence_ceiling`` is the generous backstop
    # that guarantees the loop always terminates; ``auto_fix_max_rounds`` now
    # only raises that ceiling's floor (it is no longer a terminal on its own).
    gate_convergence_ceiling: int = 20

    # Gate observation (proposal 124) — when set, ``run_quality_gates`` parses
    # each gate runner's retained output into a structured
    # ``GateRunObservation`` (``maestro/gate_observation.py``) and hangs it off
    # the ``QualityGateResult``, and a ``pytest -x`` probe that ``-x`` cut short
    # (a failing test or a collection abort — not a coverage-only shortfall,
    # whose run already completed) is re-run once *without* ``-x`` so the whole
    # failing set is enumerated. Default-off: flag-off the runner issues
    # byte-identical subprocess commands, returns a byte-identical
    # ``raw_output``, and leaves ``QualityGateResult.observation`` ``None``. No
    # consumer reads the observation yet — this increment produces the fact;
    # rewiring the terminals that will bind to it is separate work.
    gate_observation_enabled: bool = False

    # Conscious-access workspace observation (proposal 130) — when set, a
    # zero-SDK ``attend`` phase runs between ``tend_evolution_prs`` and
    # ``contemplate``: it gathers candidates from facts the iteration has
    # *already* materialised, assembles exactly one immutable ``ConsciousMoment``,
    # and appends one episode to ``conscious_episodes.jsonl`` (or the SQLite
    # ledger). Observation only — the moment is injected nowhere, changes no
    # prompt and no decision, and consults no gate. Off in base since proposal
    # 149 (no moment was ever cited — a zero-citation writer), one reversible
    # base change; True restores the burn-in. Flag-off the ``attend`` phase
    # never runs, so phase execution and state writes stay byte-identical to
    # the pre-workspace loop.
    conscious_workspace_observe_enabled: bool = False

    # Conscious-access workspace broadcast (proposal 130) — when set, the selected
    # ``ConsciousMoment`` is rendered to a bounded text block and prepended, per
    # call, to three cognitive consumers: contemplation and evolution (via
    # ``consciousness.introspect``'s ``workspace_moment`` param) and task generation
    # (by a per-call prepend in ``TaskGenerator.generate_task``, which does not
    # route through ``introspect``); each delivery records a ``BroadcastReceipt``.
    # Advisory only — the block is prompt text, grants no capability, approves no
    # plan, and consults no gate. Default-off is the rollback: flag-off every
    # consumer receives an empty block and every prompt is byte-identical to
    # today's. A no-op unless ``conscious_workspace_observe_enabled`` is also on —
    # with no ``attend`` pass there is no moment to broadcast.
    conscious_workspace_broadcast_enabled: bool = False

    # Gate execution lease (proposal 126) — when set, the ``maestro gate-lease``
    # exec wrapper measures the host (``gate_capacity.measure_host_capacity``),
    # counts the gate runs already in flight
    # (``gate_capacity.count_in_flight``), plans a bounded worker grant plus a
    # concurrency-slot budget (``gate_capacity.plan_lease``), and holds a slot
    # for the child's lifetime while running it at the granted width, so several
    # concurrent full-suite runs on one small host cannot oversubscribe it into
    # swap and manufacture red verdicts out of contention. The measured host is the
    # tighter of the host figure and the cgroup limit, so a run inside a memory-/
    # cpu-limited container sizes to the container, not the host it shares (#3760).
    # Default-on (graduated from a default-off experiment); flag-off is the
    # rollback: the wrapper takes no slot and runs the child at today's ``auto``
    # width, so the box behaves exactly as it did pre-lease. The in-process loop
    # gate readers (``run_quality_gates``, ``run_gate_check``, ``preflight``) also
    # route through the shared ``gate_capacity.lease_for_gate_run`` seam, keyed off
    # the derived ``gate_lease_dir`` property below.
    gate_lease_enabled: bool = True

    # Make the lease's memory reserve aware of *which* bound produced the measured
    # headroom. ``gate_capacity.SYSTEM_RESERVE_MIB`` is a *host* measurement — memory
    # held back for the OS and the non-agent baseline (the dashboard, background
    # services), all of which live *outside* a loop cgroup. ``memory.current``
    # already charges everything inside that cgroup, so subtracting the host reserve
    # from a cgroup-bounded reading double-counts a baseline already netted out and
    # floors the grant at one worker unless the whole cgroup sits under ~250 MiB. On:
    # the reserve is omitted when ``HostCapacity.bounded_by == "cgroup"`` (the
    # in-flight session-baseline term is kept for both bounds). Off (default): the
    # reserve applies to either bound, byte-identical to pre-flag behaviour. The
    # effect is read from the ``available_mib`` / ``bounded_by`` fields stamped on
    # every ``gate_runs.jsonl`` row — which are recorded regardless of this flag, so
    # it is flipped against measured evidence rather than an assumption.
    gate_lease_cgroup_reserve_enabled: bool = False

    # Gate admission cap: the lease's slot budget is ``cores`` by default, so an
    # 8-core host admits eight concurrent full suites that slow each other until
    # the wall-clock per-test ``pytest-timeout`` budget kills them. On: the budget
    # is ``max(1, cores // MIN_WORKERS_PER_ADMITTED_RUN)`` and a queued run waits
    # up to ``_CAPPED_ACQUIRE_WAIT_SECONDS`` (long enough to outlast one suite)
    # before failing open. Only out-of-process runs (``gate-lease`` /
    # ``gate-check``) are capped; in-loop ``in_process`` / ``preflight`` runs keep
    # the uncapped budget so a long queue never blocks the event loop.
    # Off (default): today's ``slots == cores`` and 30s wait.
    # The effect is read from the ``in_flight`` / ``admission`` / ``wait_seconds``
    # fields of every ``gate_runs.jsonl`` row and ``maestro measure-gate-contention``.
    gate_lease_admission_cap_enabled: bool = False

    # Proposal 134 gate fit: the lease owns the pytest run's budget as well as its
    # width. On, the grant carries a ``deadline_seconds`` derived from the telemetry
    # ledger at that same granted width (escalating 1.5x off the last censored run,
    # floored at ``selfcheck_timeout`` and capped at ``selfcheck_max_timeout``), and
    # only the ``tests`` gate spends it. Off (default): every gate runs under
    # ``selfcheck_timeout``, byte-identical to today.
    gate_budget_lease_owned_enabled: bool = False

    # Proposal 134 gate fit: the lease owns *which suite* a fix-loop round runs as
    # well as its width and deadline. On, ``converge_to_green``'s inner rounds probe
    # only the test modules ``gate_scope.impacted_tests`` selects from the plan's
    # ``files_affected``, with ``--no-cov``, and a green narrowed round is certified
    # by one fresh full-scope, with-coverage read before it can be a ``green``
    # terminal. Off (default): every round runs the whole suite with coverage,
    # byte-identical to today.
    gate_scope_impacted_enabled: bool = False

    # Proposal 133: consult the gate-runnability oracle *before* an implementation
    # attempt is spent. On, ``_implement_approved_plans`` refreshes
    # ``gate_environment.refresh_verdict`` once per iteration and, on a non-``SOUND``
    # verdict, implements nothing that iteration: every plan it would have selected
    # keeps its ``approved`` status and its ``failure_count``, stamped with the
    # verdict's fingerprint so the hold is keyed to the fact rather than the clock.
    # Off (default): the selector never probes and is byte-identical to today.
    gate_environment_oracle_enabled: bool = False

    # Host-scoped directory holding the lease's concurrency slot-lock files. The
    # wrapper (``cmd_gate_lease``) probes ``gate-slot-{i}.lock`` here, so the set
    # must be *shared* across every checkout on a host for the lease to bound
    # cross-checkout concurrency — not ``memory_dir / "gate_slots"``, which this
    # repo's ``memory_dir = "data"`` would scope privately per checkout, leaving
    # each checkout blind to the others. Defaults to ``~/.maestro/gate_slots``; an
    # operator or container can redirect it via ``[loop] gate_lease_slot_dir``.
    gate_lease_slot_dir: Path = field(
        default_factory=lambda: Path.home() / ".maestro" / "gate_slots"
    )

    # Host-capacity sensor (proposal 125) — when set, ``contemplate`` (and the
    # loop's hoisted iteration-start snapshot it reuses) folds a live
    # ``host_capacity.read_host_capacity()`` reading — load per core, memory and
    # swap residency, free disk on the Docker root — onto the diagnostic
    # snapshot, so the health summary names any saturated resource and the
    # blind-spot list stops reporting host capacity as unmeasured. Default-off is
    # the rollback: flag-off no reading is taken and the snapshot's
    # ``host_capacity`` stays ``None``, so the health summary is byte-identical to
    # today; ``blind_spots`` still names host capacity as an unmeasured gap (as it
    # names every other absent measurement), which is the one intended change
    # while the sensor is off. Calibrating the four thresholds against real
    # saturation is a separate, reversible flip once the sensor has merged.
    host_capacity_sensor_enabled: bool = False

    # Runtime contract enforcement (proposal 141 step 8). ``preflight`` and
    # ``_build_agent`` (every agent-building command, ``serve`` included)
    # always fold ``deploy/runtime_contract.toml`` against the live
    # environment offline; on, a required violation exits non-zero (so a
    # safe-deploy smoke rolls back), off, each violation is a WARNING only.
    runtime_contract_enforced: bool = False

    # Cap on cross-iteration retries of a single ``failed`` plan. Once
    # ``failure_count`` reaches this value the plan is no longer picked up
    # by ``_implement_approved_plans`` and a terminal GitHub issue is
    # opened on the final attempt.
    max_implementation_attempts: int = 3

    # Attempt-disposition oracle (proposal 131, RESUME arm). When on, a
    # post-implementation quality-gate convergence that stalls *but is strictly
    # progressing* (its failing-identity set shrinking attempt over attempt) is
    # routed through ``attempt_disposition.classify_attempt``: the evolution
    # branch is preserved, the prior attempt's failing-identity baseline and gate
    # memory are carried into the next implementation prompt, and the plan
    # re-admits and continues instead of cold-restarting from ``main`` — bounded
    # by ``max_resume_attempts`` so a plan that never converges still terminates.
    # Off (default): the implementation path and prompts are byte-identical to
    # today (a stall demotes to ``failed`` and any retry cold-restarts).
    attempt_disposition_enabled: bool = False

    # Resume budget for the proposal-131 RESUME arm: the maximum number of times
    # a single plan may preserve its branch and continue a progressing stall
    # before the disposition oracle degrades to the ordinary demote. Read only
    # when ``attempt_disposition_enabled`` is on.
    max_resume_attempts: int = 2

    # Grounded contemplation (proposal 132) — when set, the priced contemplation
    # (slot-A) reflection session is built with the bounded, structurally
    # read-only ``reflection_retrieval`` surface (``build_reflection_tool_set``)
    # instead of ``tools=[]``, so a falsifiable bet can *pull* the one fact it
    # hinges on rather than reasoning only over what the loop pushed. Off
    # (default): the session stays ``tools=[]`` and byte-identical to today. This
    # flag is inert on landing — no code reads it yet; it exists only so the
    # subsequent session-wiring increment can gate itself behind it.
    grounded_reflection_enabled: bool = False

    # Grounded planning (proposal 143) — when set, ``propose_plan``'s single-use
    # session carries the bounded read-only ``tree`` surface
    # (``tree_retrieval.build_bounded_tree_tool_set``) so the planner can read the
    # files and symbols its plan cites before claiming them. Degrades to
    # tool-free under ``budget_throttle_active``. Requires
    # ``sdk_single_use_evolution_calls_enabled`` (validated below): only the
    # single-use arm can carry per-call MCP options. Off: byte-identical.
    grounded_planning_enabled: bool = False
    # Which planner callers the grounded-planning flag grounds (proposal 143).
    # Closed set ``GROUNDED_PLANNING_CALLERS``; validated even with the flag off
    # so a typo fails loud before the flip. The default is ``propose_plan``
    # alone (``reflect`` was deleted, proposal 155 step 4); adding a caller is
    # a separate canary.
    grounded_planning_callers: list[str] = field(
        default_factory=lambda: ["propose_plan"],
    )
    # When on (and grounded planning active), the bounded ``tree`` surface the
    # planner carries gains a sixth pure tool, ``search_rejections``, backed by
    # the rejected-plan corpus ``evolution.py`` reads from memory — so the
    # planner can query the specific ideas already refused (and their reasons)
    # at mint time, not just the aggregate category counts
    # ``read_reality_inventory`` surfaces. Off: the five-tool set is unchanged.
    grounded_planning_rejection_tool_enabled: bool = False

    # Stale-repeat bet guard. When on, ``contemplate()`` computes the set of
    # loss-condition strings already staked at or above the repeat threshold
    # without ever scoring ``"correct"`` (the "comfort bet" signature — an
    # always-winnable/never-scoring wager re-run verbatim as a hiding place) and
    # passes it into the priced-reflection prompt, which names each under a "do not
    # reuse" block so the next bet cannot verbatim-repeat a stale loser. Matching
    # is whitespace/case-normalised only, so it curbs the verbatim treadmill, not a
    # reworded restatement. Off (default): the banned set stays empty and the
    # rendered prompt is byte-identical to today. Read in ``agent.contemplate``.
    stale_repeat_bet_guard_enabled: bool = False

    # Cap on plans implemented per loop iteration. ``_implement_approved_plans``
    # selects every eligible plan (approved + failed-retry under
    # ``max_implementation_attempts``) and processes only the first ``cap``
    # entries in sort order; the rest drain across subsequent iterations. The
    # default (1) bounds blast radius when a queue accumulates from prior
    # skipped iterations (budget throttle, crash-resume past the phase,
    # standby) or batch dashboard / Telegram approvals.
    max_implementations_per_iteration: int = 1

    # Experimental concurrency cap on open evolution PRs. When the number
    # of open evolution PRs (counted by ``tend_evolution_prs``) reaches
    # this cap, ``_implement_approved_plans`` returns early without
    # implementing any plans and steers ``next_task_directive`` toward
    # integration work. ``<= 0`` disables the gate entirely.
    max_open_evolution_prs: int = 5

    # Long-context implementation session — when ``sdk_model_long_context``
    # is set AND this flag is true, the per-plan implementation session
    # runs on the long-context model and bash output capture uses the
    # enlarged cap so verbose build/test logs survive into the next
    # step's prior_context. Disabled by default because it raises cost on
    # every implementation step. The self_review and conflict-resolution
    # call sites read ``sdk_model_long_context`` directly and do not need
    # this flag — they fire rarely enough that always-on long-context
    # there is acceptable when the model is set.
    long_context_implementation_enabled: bool = False

    # Experiment cadence — every Nth task, the generation prompt
    # mechanically requires a hypothesis-driven experiment where
    # success means "data collected and analyzed" regardless of
    # confirmation.  Uses gates-over-values: the counter fires
    # whether selection pressure toward safe engineering tasks wants
    # it to or not.  Classified as experiment priority — may be
    # reverted if the LLM games the format.
    experiment_cadence: int = 5

    # Promotable-backlog pressure — when the number of successful task
    # branches not yet cited by any plan's ``source_task_id`` reaches
    # this threshold, a PROMOTABLE BACKLOG section is injected into
    # the task-generation prompt listing top-3 candidates. Uses a wide
    # task window (50) to capture the full backlog. Experiment priority
    # — revert or upgrade to gate if backlog does not decrease within
    # 30 iterations.
    promotable_backlog_threshold: int = 3

    # Promotable-backlog directive bridge — directive-bridge complement to
    # the advisory PROMOTABLE BACKLOG / Integration Opportunity prompt
    # hints. When the uncited-branch count reaches
    # ``promotable_backlog_threshold`` and no directive is pending,
    # auto-fills ``next_task_directive`` naming the oldest uncited branch
    # so the backlog signal actuates behavior rather than only informing
    # it. Self-extinguishing once branches are promoted. Experimental —
    # revert if the backlog does not decrease within 10 iterations.
    promotable_backlog_directive_enabled: bool = True

    # Promotable-branch hint — surfaces successful unmerged task
    # branches in the task-generation prompt so the LLM can propose
    # branch-promotion tasks (integration over creation).  Zero SDK
    # cost decision; only adds context to the existing prompt.
    promotable_branch_hint_enabled: bool = True
    promotable_branch_max_shown: int = 5

    # Prior-task context injection — injects the most recent task's
    # key_findings (or result_summary fallback, any outcome) into the
    # task-generation prompt so the LLM can compound on or learn from
    # prior work. Zero SDK cost context injection; 500-char cap.
    prior_task_context_enabled: bool = True

    # Integration cadence — when no task in the last N tasks performed
    # integration work (triage, promote branch, etc.), injects a
    # concrete directive into next_task_directive before the LLM call.
    # Self-extinguishing: once integration work happens, the cadence
    # resets and the gate stays quiet.
    integration_cadence: int = 5

    # Investigation-streak directive — when the last N consecutive tasks
    # are all investigation-shaped (measurement/investigation/other), auto-fills
    # next_task_directive requiring construction-shaped work. Self-
    # extinguishing: one construction task resets the streak. Experimental.
    investigation_streak_threshold: int = 3
    investigation_streak_enabled: bool = True

    # Investigation-rate directive — windowed complement to the streak
    # check. When the fraction of investigation-shaped tasks in the last
    # ``investigation_rate_window`` tasks exceeds
    # ``investigation_rate_threshold``, auto-fills next_task_directive with
    # a construction requirement. Catches a *distributed* investigation
    # pattern (mechanical tasks interleaved) that the consecutive-streak
    # check misses. Self-extinguishing. Experimental.
    investigation_rate_window: int = 10
    investigation_rate_threshold: float = 0.4
    investigation_rate_enabled: bool = True

    # Branchless-streak directive — when the last N consecutive tasks all
    # produced no branch (analysis-only work, where I am sole judge of
    # completion), auto-fills ``next_task_directive`` forcing the next task
    # to target specific code files and produce a branch with file changes.
    # Uses the ``branch`` field as a proxy for code-change. Self-
    # extinguishing: one branch-producing task resets the streak.
    # Experiment priority — may be reverted if too noisy.
    branchless_streak_threshold: int = 3

    # Readmission→construction directive bridge — actuates the landed
    # ``measure_pr_readmission_rate`` sensor. When already-merged PRs keep
    # re-lapping as remediation tasks at a rate at/above
    # ``readmission_construction_ratio_threshold`` and no higher-precedence
    # gate set a directive, auto-fills ``next_task_directive`` forcing the next
    # task into a construction shape (build something that lands) rather than
    # another remediation lap. Lowest directive precedence; self-extinguishing
    # via the one-shot clear-on-accepted-task. Default-off, experimental.
    readmission_construction_directive_enabled: bool = False
    readmission_construction_ratio_threshold: float = 0.5

    # Uncalled-symbol quality gate — post-self-review blocking gate that
    # rejects evolution steps shipping new definitions (``def``/``class``)
    # without at least one non-test call site in the codebase.
    # ``uncalled_symbol_max_allowed`` sets the threshold: steps introducing
    # more uncalled symbols than this count are failed. 0 = every new
    # symbol must be wired. The gate reuses the same diff-parsing and
    # ``git grep`` logic as ``_find_uncalled_symbols``.
    uncalled_symbol_gate_enabled: bool = True
    uncalled_symbol_max_allowed: int = 0

    # Acceptance-criteria alignment — when enabled, the code-writing phases
    # (``Evolution.implement`` and ``Evolution._run_auto_fix_query``) receive
    # the plan's ``acceptance_criteria`` block so the writer aims at the
    # binding contract from the first commit rather than first meeting it at
    # self-review. Inert for plans without criteria.
    criteria_alignment_enabled: bool = True

    # Quiet-skip gate — pre-call blocking gate in ``generate_task`` that
    # skips the entire LLM call when the task queue already holds items
    # and no external requests (user or PR) are pending.  Creates natural
    # drain-then-refill cycles: the queue empties via ``_run_task_phase``,
    # then ``generate_task`` runs with fresher context.
    quiet_skip_enabled: bool = True

    # Compound-scope post-call gate — rejects self-generated tasks whose
    # goal combines a creation verb (build/add/create/implement/write/
    # introduce/extend) with a connection verb (wire/hook/plug/inject/
    # integrate/connect) across a conjunction marker.  Deterministic regex
    # check — zero SDK cost.  Logs to gate_activations.jsonl.
    compound_scope_gate_enabled: bool = True

    # Phantom-premise post-call gate — refuses a self-generated goal that
    # claims an *existing* defect in a ``.py`` path absent from the tree,
    # while admitting a goal whose framing is to *create* that path.  Reads
    # the missing references the advisory premise report already computes;
    # deterministic regex — zero SDK cost.  Logs to gate_activations.jsonl.
    phantom_premise_gate_enabled: bool = False

    # Investigation-observable post-call gate — steers a self-generated task
    # that is investigation-framed (reading/tracing/understanding an existing
    # mechanism) AND commits to creating a new artifact file, while naming no
    # existing observable instrument signal to read/report, toward reading and
    # reporting a named signal instead. Sets next_task_directive into an empty
    # slot only; logs to gate_activations.jsonl. Zero SDK cost. Experiment.
    investigation_observable_gate_enabled: bool = True

    # Plan-premise grounding — when enabled, propose_plan asks the model to
    # attach a grounding probe for a plan premised on a concrete defect or a
    # named new mechanism, and submit_for_review confirms that probe against a
    # fresh reading of the tree as its first guard: a refuted premise is recorded
    # obsolete before a human approval is spent (proposal 114). Default off; the
    # flag-off path is byte-identical to today.
    plan_grounding_enabled: bool = False

    # Generator reality grounding — when enabled, propose_plan asks the model to
    # declare the measured problem it addresses (``problem_ref``) and the names it
    # will newly define (``new_capability_names``), and submit_for_review clears
    # both against the reality inventory as its first guard, recording a plan
    # that re-proposes a shipped capability or cites an unmeasured problem
    # obsolete before a human is notified (proposal 136). Default off; the
    # flag-off path is byte-identical to today.
    generator_reality_grounding_enabled: bool = False

    # Models — overrides the SDK default for every query.
    # Effort stays per-call; this only selects which neural net runs.
    # Empty string = inherit SDK default (safe fallback).
    sdk_model: str = model_profiles.DEFAULT_MODEL

    # Fallback model when primary is unavailable. Empty string = disabled.
    sdk_fallback_model: str = ""

    # Targeted opt-in 1M-context model variant. Empty string = disabled
    # (the default). When non-empty, three call sites switch to this
    # model with enlarged char/budget caps: self_review diff, conflict
    # resolution, and (when ``long_context_implementation_enabled`` is
    # also true) the per-plan implementation session. Maestro injects
    # the SDK's ``context-1m-2025-08-07`` beta automatically on every
    # call that uses this model.
    sdk_model_long_context: str = ""

    # Per-call token budget. 0 = unlimited. When > 0, must be >= 1024.
    sdk_task_budget_tokens: int = 0

    # Provider selection (proposal 058). "anthropic" (default) runs today's
    # exact path; other names must be registered in
    # :mod:`maestro.providers.registry` to be selectable (validated at
    # ``cli.main`` time, not here). See ``VALID_SDK_PROVIDERS``. (The retired
    # proposal-056 ``[sdk] backend`` field was folded into this provider
    # registry in 056 Step 11.)
    sdk_provider: str = "anthropic"

    # Per-phase provider overrides (proposal 058). Maps a phase name (one of
    # ``SDK_PHASE_NAMES``) to a provider name; phases absent here fall back to
    # ``resolved_sdk_provider()``. Step 6 parses, validates, and exposes the
    # resolver (``provider_for_phase``); threading it into call sites is Step 7.
    sdk_phase_providers: dict[str, str] = field(default_factory=dict)

    # Work target (proposal 107) — the single owner for which repo, which gate
    # path atoms, and which framing the loop points its effort at. Every facet
    # defaults to today's self values, so a build with no ``[work]`` section is
    # byte-for-byte identical to current behaviour. Assembled into a
    # :class:`maestro.work_target.WorkTarget` by ``work_target()``, through
    # which every facet is consumed.
    # ``work_repo_workspace`` / ``work_github_repo`` fall back to ``workspace`` /
    # ``github_repo`` when empty.
    work_focus_profile: str = "self_evolution"
    work_task_framing: str = "self_evolution"
    work_repo_workspace: str = ""
    work_github_repo: str = ""
    work_gate_source_path: str = "maestro/"
    work_gate_tests_path: str = "tests/"
    work_gate_lint_extra_paths: list[str] = field(default_factory=lambda: ["scripts/"])

    # cli_paced provider pacing + idle overrides (proposal 056 Step 12).
    # ``[sdk.cli_paced]`` is a nested table holding ``maestro_sdk.Cadence`` knob
    # overrides (keys in ``VALID_CLI_PACED_CADENCE_KEYS``) plus
    # ``stream_idle_timeout_seconds``. An empty ``sdk_cli_paced_cadence`` keeps the
    # documented ``Cadence()`` defaults; a ``0`` idle timeout keeps the
    # ``maestro_sdk.QueryOptions`` built-in idle deadline. Only consulted when the
    # cli_paced provider is selected (``configure_cli_paced`` in ``cli.main``); the
    # CLI path deliberately does *not* inherit the agent-side
    # ``sdk_stream_idle_timeout_seconds`` global.
    sdk_cli_paced_cadence: dict[str, float] = field(default_factory=dict)
    sdk_cli_paced_stream_idle_timeout_seconds: float = 0.0

    # cli_paced slash-command metrics probe (proposal 062 Step 6). Also under
    # ``[sdk.cli_paced]`` (keys ``probe_metrics_enabled`` / ``probe_metrics_every``);
    # default-off, every-turn cadence when enabled. Wired into
    # ``configure_cli_paced`` alongside the cadence/idle overrides above, so the
    # default ``anthropic`` path never consults them.
    sdk_cli_paced_probe_metrics_enabled: bool = False
    sdk_cli_paced_probe_metrics_every: int = 1

    # cli_paced per-CLI-version marker pack (proposal 075 Step 4). Also under
    # ``[sdk.cli_paced]`` (key ``marker_pack``): ``"none"`` (default — today's
    # empty-marker behaviour), ``"auto"`` (probe ``claude --version`` at startup
    # and select the newest covering registered pack, warning and proceeding
    # pack-less when none covers), or a registered pack name (validated in
    # ``validate()`` — a typo must not silently mean "no markers"). Resolved by
    # ``resolve_marker_pack`` at the ``configure_cli_paced`` call site.
    sdk_cli_paced_marker_pack: str = "none"

    # Session reuse — sequential step tiers share a ClaudeSDKClient session
    # for prompt cache reuse across the full conversation prefix.
    enable_session_reuse: bool = True

    # Per-model pricing in USD per million tokens. Indexed by model name
    # exactly as it appears in the SDK ``model`` field. Each value is a
    # dict with optional keys ``input``, ``output``, ``cache_creation``,
    # ``cache_read`` (defaulting to 0.0). Empty table disables cost
    # estimation on the early-exit predicate path — the SDK still
    # records reported cost from ``ResultMessage`` when present.
    # The default is a projection of ``maestro.model_profiles`` — the single
    # owner of per-model rates — so test fixtures (and ``Config()`` callers
    # without TOML loaded) get a populated estimator. A non-empty TOML
    # ``[sdk.pricing.*]`` group still replaces this default wholesale.
    #
    # The ``[1m]`` suffix on a model key (e.g. ``claude-opus-4-7[1m]``)
    # is a Maestro-internal pricing label — *not* an Anthropic model
    # identifier. The 1M-token context window is enabled via the
    # ``context-1m-2025-08-07`` beta header on the standard model, and
    # ``maestro.sdk._label_long_context_pricing`` post-processes
    # ``QueryUsage.model`` to append this suffix when the beta is
    # active so the lookup hits the 1M-tier row. Never pass the
    # ``[1m]``-suffixed string to ``[models] long_context`` — Anthropic
    # would reject it as an unknown model.
    sdk_pricing: dict[str, dict[str, float]] = field(
        default_factory=model_profiles.pricing_table
    )

    # Artifact review — consumed by the review_artifacts phase in
    # proposal 031. ``artifact_review_enabled`` gates the phase entirely.
    artifact_review_enabled: bool = True
    artifact_review_every: int = 20
    artifact_review_batch_size: int = 3
    artifact_review_max_file_bytes: int = 4096
    artifact_review_max_files_per_task: int = 10
    artifact_review_max_bundle_bytes: int = 12288

    # Publication — consumed by proposals 032 (approval surface) and 033
    # (execution pipeline). ``publication_owner`` names the GitHub namespace
    # published repos are created under. Empty remains a supported sentinel
    # meaning "derive the authenticated login via ``gh api user``".
    publication_enabled: bool = True
    publication_owner: str = "thomas-sodon"
    publication_copyright_holder: str = "Thomas Sodon"
    publication_license: str = "MIT"
    publication_secret_scan_enabled: bool = True
    publication_executing_stale_timeout_seconds: int = 600
    publication_push_retry_limit: int = 3

    # Gap-fuelled evolution (``[evolution_fuel]``, proposal 155): the most
    # plans one gap issue may fuel, and the hours a red ``main`` is tolerated
    # before it becomes a gap.
    evolution_fuel_max_plans_per_gap: int = 2
    evolution_fuel_red_main_grace_hours: float = 12.0
    # GitHub logins whose label/close acts are the operator's (never the
    # loop's own login, refused at startup).
    evolution_fuel_operator_github_logins: list[str] = field(default_factory=list)

    # MCP servers
    context7_mcp_enabled: bool = False
    context7_mcp_command: str = "npx"
    context7_mcp_args: list[str] = field(
        default_factory=lambda: ["-y", "@upstash/context7-mcp"],
    )

    # Serena — opt-in symbol-level code retrieval (LSP-backed). Default off and
    # byte-identical to today while off. The args pin an exact package version so
    # the server's tool inventory cannot drift under us; its mutating tools are
    # denied by name via ``external_mcp_disallowed_tools()`` so the server stays
    # read-only and cannot bypass the ``Bash``-scoped action gate.
    serena_mcp_enabled: bool = False
    serena_mcp_command: str = "uvx"
    serena_mcp_args: list[str] = field(
        default_factory=lambda: [
            "--from", "serena-agent==1.5.3", "serena", "start-mcp-server",
        ],
    )

    def _check_cross_field_and_subtable(self) -> None:
        """Enforce the guards a per-field ``Field(...)`` constraint cannot express.

        Holds the cross-field/relational guards (one field bounded by another),
        the semantic single-field guards not expressible as a plain range (a real
        IANA zone; the disjoint ``0``-or-``>= 1024`` task budget; ``sdk_phase_providers``
        phase/provider-name membership), and the ``[sdk.cli_paced]`` sub-table
        guards (fields hand-parsed by ``config_settings`` that never enter the
        declarative section models). :meth:`validate` calls this after its
        flat-scalar ladder, and
        :func:`~maestro.config_settings.build_config_via_pydantic` calls it on the
        assembled ``Config`` so the declarative load path enforces the complete
        rejection surface — the section-model ``Field(...)`` constraints plus these
        guards — from one definition, with no drift. Every raise message is
        identical to the inline block it was lifted from.
        """
        def _check_int(val: object) -> bool:
            """Return True if *val* is a real int (not bool)."""
            return isinstance(val, int) and not isinstance(val, bool)

        if self.sdk_call_policy_resolution_enabled:
            # Slot B and the implementation session carry turns at a tier
            # effort different from their opener's unless these are on, so
            # the seam's mismatch raise would fire in production.
            for prereq in (
                "sdk_single_use_evolution_calls_enabled",
                "self_review_own_session_enabled",
            ):
                if not getattr(self, prereq):
                    raise ValueError(
                        f"sdk_call_policy_resolution_enabled requires "
                        f"{prereq} to be enabled"
                    )
        if (
            self.grounded_planning_enabled
            and not self.sdk_single_use_evolution_calls_enabled
        ):
            raise ValueError(
                "grounded_planning_enabled requires "
                "sdk_single_use_evolution_calls_enabled to be enabled"
            )
        unknown_callers = [
            c for c in self.grounded_planning_callers
            if c not in GROUNDED_PLANNING_CALLERS
        ]
        if unknown_callers:
            raise ValueError(
                "grounded_planning_callers contains unknown callers: "
                f"{unknown_callers!r}. Valid: {sorted(GROUNDED_PLANNING_CALLERS)!r}"
            )
        if len(set(self.grounded_planning_callers)) != len(
            self.grounded_planning_callers,
        ):
            raise ValueError(
                "grounded_planning_callers contains duplicate entries: "
                f"{self.grounded_planning_callers!r}"
            )
        if (
            not _check_int(self.orphaned_cure_retire_age_cycles)
            or self.orphaned_cure_retire_age_cycles
            <= self.orphaned_cure_age_cycles
        ):
            raise ValueError(
                f"orphaned_cure_retire_age_cycles must be an int strictly "
                f"greater than orphaned_cure_age_cycles "
                f"({self.orphaned_cure_age_cycles!r}), got "
                f"{self.orphaned_cure_retire_age_cycles!r}"
            )
        # selfcheck_max_timeout: the hard cap a lease-derived pytest budget may
        # escalate to must not sit below the floor it starts from.
        if (
            not _check_int(self.selfcheck_max_timeout)
            or self.selfcheck_max_timeout < self.selfcheck_timeout
        ):
            raise ValueError(
                "selfcheck_max_timeout must be an int >= selfcheck_timeout "
                f"({self.selfcheck_timeout!r}), got "
                f"{self.selfcheck_max_timeout!r}"
            )
        # sdk_tool_stream_idle_timeout_seconds: the idle net a tool-using caller
        # relies on must sit strictly above the largest budget a single gate run
        # may spend, or a gate permitted to run for ``selfcheck_max_timeout``
        # seconds inside one Bash call has its stream declared stalled first.
        # ``<= 0`` keeps its documented disable semantics — a net that never
        # fires has no ordering to violate.
        if (
            self.sdk_tool_stream_idle_timeout_seconds > 0
            and self.sdk_tool_stream_idle_timeout_seconds
            <= self.selfcheck_max_timeout
        ):
            raise ValueError(
                "sdk_tool_stream_idle_timeout_seconds must be strictly greater "
                "than selfcheck_max_timeout "
                f"({self.selfcheck_max_timeout!r}) when enabled, got "
                f"{self.sdk_tool_stream_idle_timeout_seconds!r}"
            )
        # archive_threshold_days: an *enabled* tiered-memory rotation threshold
        # must exceed the longest hot-read window over every file
        # ``Memory.archive()`` rotates, or it sheds still-in-window rows to
        # cold storage no hot reader consults. The floor is the widest of
        # three windows: the dashboard's
        # ``window_hours`` cap over
        # ``cost_samples.jsonl`` (``le=168.0`` on ``/api/cost-rates`` and
        # ``/api/budget`` in ``dashboard/routes.py``) — below it the budget /
        # cost-rate view silently under-reports spend; the operator-
        # configurable burn-rate ``budget_rate_window_hours`` over that same
        # file; and ``loop_plan_review_expiry_days`` over
        # ``evolution_plans.jsonl``: ``archive()`` is status-scoped (only
        # ``ARCHIVABLE_PLAN_STATUSES`` rotate), so a ``pending_review`` plan is
        # already held hot by status — this floor is the second, independent
        # guard, keeping the expiry sweep's horizon inside the hot window even
        # if that status set is ever widened. Only ``0``
        # keeps its documented disabled semantics (no rotation, no window to
        # violate); the int-ness test runs first so a ``bool`` cannot pose as
        # either an enabled or a disabled threshold.
        _archive_floor_hours = max(
            168.0,
            self.budget_rate_window_hours,
            max(self.loop_plan_review_expiry_days, 0) * 24.0,
        )
        _archive_min_days = int(_archive_floor_hours // 24) + 1
        if not (
            _check_int(self.archive_threshold_days)
            and (
                self.archive_threshold_days == 0
                or self.archive_threshold_days * 24 > _archive_floor_hours
            )
        ):
            raise ValueError(
                "archive_threshold_days must be 0 (disabled) or an int of at "
                f"least {_archive_min_days} days — strictly more than the "
                f"widest hot-read window ({_archive_floor_hours!r} hours) — "
                f"got {self.archive_threshold_days!r}"
            )
        # sdk_rate_limit_idle_max_probe_interval: must be >= probe interval
        if (
            not _check_int(self.sdk_rate_limit_idle_max_probe_interval)
            or self.sdk_rate_limit_idle_max_probe_interval
            < self.sdk_rate_limit_idle_probe_interval
        ):
            raise ValueError(
                "sdk_rate_limit_idle_max_probe_interval must be >= "
                "sdk_rate_limit_idle_probe_interval, got "
                f"{self.sdk_rate_limit_idle_max_probe_interval!r}"
            )
        # sdk_task_budget_tokens: 0 (disabled) or >= 1024
        if 0 < self.sdk_task_budget_tokens < 1024:
            raise ValueError(
                "sdk_task_budget_tokens must be 0 (disabled) or >= 1024, got "
                f"{self.sdk_task_budget_tokens!r}"
            )
        if self.standby_start_hour_utc == self.standby_end_hour_utc:
            raise ValueError(
                "standby_start_hour_utc and standby_end_hour_utc must differ "
                "(equal hours would mean always-on or always-off)"
            )
        if (
            not isinstance(self.peak_throttle_timezone, str)
            or not self.peak_throttle_timezone.strip()
        ):
            raise ValueError(
                "peak_throttle_timezone must be a non-empty IANA zone "
                f"name, got {self.peak_throttle_timezone!r}"
            )
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.peak_throttle_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                "peak_throttle_timezone must be a valid IANA zone name "
                f"(e.g. 'America/Los_Angeles', 'UTC'), got "
                f"{self.peak_throttle_timezone!r}"
            ) from exc
        if (
            self.peak_throttle_start_hour
            == self.peak_throttle_end_hour
        ):
            raise ValueError(
                "peak_throttle_start_hour and peak_throttle_end_hour "
                "must differ (equal hours would mean always-on or "
                "always-off)"
            )
        for phase, name in self.sdk_phase_providers.items():
            if phase not in SDK_PHASE_NAMES:
                raise ValueError(
                    f"sdk_phase_providers has unknown phase {phase!r}; valid "
                    f"phases are {sorted(SDK_PHASE_NAMES)}"
                )
            if name not in VALID_SDK_PROVIDERS:
                raise ValueError(
                    f"sdk_phase_providers[{phase!r}] must be one of "
                    f"{sorted(VALID_SDK_PROVIDERS)}, got {name!r}"
                )
        # Per-phase routing is implemented at the session layer: the provider is
        # resolved and the session tagged when a phase's session is created, and
        # follow-up turns route via that tag (proposal 058 Step 6 runtime half).
        # With session reuse off the loop's phases fall back to stateless
        # one-shot ``query()`` calls, and all but the implementation lineage
        # carry no phase, so an override could not take effect — fail loud here
        # rather than silently ignore it. The exception does not weaken the
        # check: issue #2914 tagged ``implement`` / ``self_review`` /
        # ``auto_fix``'s sessionless arms ``phase="implementation"`` so their
        # action-gate routing binds directly to the phase
        # ``validate_action_gate_coverage`` checks, not so the override becomes
        # honourable — every other phase on this path would still ignore it.
        # Calls that run sessionless in *every* config (the scope-gate replan
        # and chain emission, #4101) tag their phase directly, so reuse-on
        # routing never leaks them to the global provider.
        if self.sdk_phase_providers and not self.enable_session_reuse:
            raise ValueError(
                "per-phase [sdk] <phase>_provider overrides require "
                "enable_session_reuse=true: runtime per-phase routing resolves "
                "and tags the provider when a phase's session is created, so the "
                "session-reuse-off (stateless query) path cannot honour an "
                "override"
            )
        # cli_paced Cadence + stream-idle overrides (proposal 056 Step 12) —
        # schema-level gate. Keys must name a real Cadence knob and every value
        # (and the idle timeout) must be a finite, non-negative number so a typo,
        # a NaN, or a negative pacing knob fails at config load, not when the
        # provider builds its Cadence at first query. Negatives are rejected
        # symmetrically with the idle timeout below: a negative ``min_key_ms``
        # would flow into ``Cadence.delay_for`` and make ``asyncio.sleep`` raise.
        for key, value in self.sdk_cli_paced_cadence.items():
            if key not in VALID_CLI_PACED_CADENCE_KEYS:
                raise ValueError(
                    f"sdk_cli_paced_cadence has unknown key {key!r}; valid keys "
                    f"are {sorted(VALID_CLI_PACED_CADENCE_KEYS)}"
                )
            if isinstance(value, bool) or \
               not isinstance(value, (int, float)) or \
               math.isnan(value) or math.isinf(value) or value < 0:
                raise ValueError(
                    f"sdk_cli_paced_cadence[{key!r}] must be a finite "
                    f"non-negative number, got {value!r}"
                )
            # ``cols``/``rows`` are ``int`` fields on ``maestro_sdk.Cadence``; a
            # fractional value would reach the pty winsize as a float. Reject it
            # at load (the value is already finite/non-negative here), keeping
            # this gate the complete hard barrier for programmatic construction.
            if key in INT_CLI_PACED_CADENCE_KEYS and value != int(value):
                raise ValueError(
                    f"sdk_cli_paced_cadence[{key!r}] must be a whole number "
                    f"(it is an int Cadence field), got {value!r}"
                )
            # ``thinking_pause_prob`` / ``size_jitter`` are unit-interval [0, 1]
            # knobs; the lower bound is the non-negative gate above, the upper
            # bound is here so a value > 1 (which would distort pacing rather
            # than error in maestro_sdk) fails at config load.
            if key in UNIT_INTERVAL_CLI_PACED_CADENCE_KEYS and value > 1:
                raise ValueError(
                    f"sdk_cli_paced_cadence[{key!r}] must be in [0, 1] "
                    f"(it is a probability/fraction Cadence field), got {value!r}"
                )
        idle = self.sdk_cli_paced_stream_idle_timeout_seconds
        if isinstance(idle, bool) or not isinstance(idle, (int, float)) or \
           math.isnan(idle) or math.isinf(idle) or idle < 0:
            raise ValueError(
                "sdk_cli_paced_stream_idle_timeout_seconds must be a finite "
                f"non-negative number, got {idle!r}"
            )
        # cli_paced slash-probe knobs (proposal 062 Step 6). The enable flag must
        # be a real bool and the cadence a whole number >= 1 — the provider reads
        # a non-positive cadence as "every turn" defensively, but config is the
        # hard gate, so a 0/negative interval fails loud at load time instead of
        # silently turning into the most expensive cadence.
        if not isinstance(self.sdk_cli_paced_probe_metrics_enabled, bool):
            raise ValueError(
                "sdk_cli_paced_probe_metrics_enabled must be a bool, got "
                f"{self.sdk_cli_paced_probe_metrics_enabled!r}"
            )
        probe_every = self.sdk_cli_paced_probe_metrics_every
        if isinstance(probe_every, bool) or not isinstance(probe_every, int) or \
           probe_every < 1:
            raise ValueError(
                "sdk_cli_paced_probe_metrics_every must be an integer >= 1, "
                f"got {probe_every!r}"
            )
        # cli_paced marker-pack mode (proposal 075 Step 4). The value must be a
        # non-empty string; a *named* pack is checked against the maestro_sdk
        # registry only when cli_paced is actually selected (globally or
        # per-phase), keeping the default anthropic path free of the
        # maestro_sdk import — the same lazy posture as cli.main's
        # configure_cli_paced call site. An unknown name fails loud here: a
        # typo'd pack must not silently mean "no markers".
        marker_pack = self.sdk_cli_paced_marker_pack
        if not isinstance(marker_pack, str) or not marker_pack.strip():
            raise ValueError(
                'sdk_cli_paced_marker_pack must be "auto", "none", or a '
                f"registered pack name, got {marker_pack!r}"
            )
        if marker_pack not in ("auto", "none") and "cli_paced" in {
            self.resolved_sdk_provider(), *self.sdk_phase_providers.values()
        }:
            from maestro_sdk import MARKER_PACKS, get_marker_pack
            if get_marker_pack(marker_pack) is None:
                raise ValueError(
                    f"sdk_cli_paced_marker_pack names unknown pack "
                    f"{marker_pack!r}; registered packs: "
                    f'{[pack.name for pack in MARKER_PACKS]} (or "auto"/"none")'
                )

        # Model admission (proposal 140): every configured Anthropic-backed
        # model must carry a ``model_profiles`` profile, since Maestro steers
        # thinking through per-call effort alone and a budget-only model would
        # silently run without thinking. Empty keeps its inherit/disabled
        # meaning; a pure-openai routing set has its own model admission.
        if {"anthropic", "cli_paced"} & {
            self.resolved_sdk_provider(), *self.sdk_phase_providers.values()
        }:
            for field_name in (
                "sdk_model", "sdk_fallback_model", "sdk_model_long_context",
            ):
                model = getattr(self, field_name)
                if model and model_profiles.get_profile(model) is None:
                    raise ValueError(
                        f"{field_name} names {model!r}, which has no model "
                        "profile; admitted models: "
                        f"{sorted(model_profiles.MODEL_PROFILES)} (budget-only "
                        "models are not admitted)"
                    )

    def validate(self) -> None:
        """Delegate to the cross-field / sub-table guard.

        The flat-scalar range, length, ``isinstance``-bool, and single-field
        membership guards this method used to hold were retired in proposal
        106: they are reproduced declaratively by the ``config_settings``
        section models (``Field(...)`` constraints, the ``_NumericInt`` /
        ``_NumericFloat`` numeric-bool before-validators, and the
        ``_check_framing`` / ``_check_provider`` field validators) on the
        ``build_config_authoritative`` load path, which stopped calling
        ``validate()`` in an earlier increment. What remains — the relational,
        semantic single-field, and ``[sdk.cli_paced]`` sub-table guards a
        per-field constraint cannot express — lives in
        ``_check_cross_field_and_subtable``, which the load path calls directly
        and this method now delegates to, so the two share one definition and
        cannot drift.

        Raises ``ValueError`` if a cross-field or sub-table guard fails.
        """
        self._check_cross_field_and_subtable()

    def mcp_servers_config(self) -> dict[str, dict[str, Any]] | None:
        """Build MCP server configuration for SDK queries.

        Returns ``None`` when no MCP servers are enabled. GitHub access is
        deliberately absent: it goes through the control-plane code host, not
        an MCP server whose repo-writing tools would bypass the ``Bash``-scoped
        action gate.
        """
        servers: dict[str, dict[str, Any]] = {}

        # Context7 MCP — no auth required
        if self.context7_mcp_enabled:
            servers["context7"] = {
                "command": self.context7_mcp_command,
                "args": list(self.context7_mcp_args),
            }

        # Serena MCP — no auth required; read-only via disallowed_tools
        if self.serena_mcp_enabled:
            servers["serena"] = {
                "command": self.serena_mcp_command,
                "args": list(self.serena_mcp_args),
            }

        return servers or None

    def external_mcp_disallowed_tools(self) -> list[str] | None:
        """Tool-denial list for sessions that attach external MCP servers.

        When the read-only symbol-retrieval server (``serena``) is enabled, its
        entire tool surface auto-exposes under ``bypassPermissions``. Denying its
        mutating / shell-exec tools by qualified ``mcp__serena__*`` name keeps the
        server read-only so it cannot bypass the ``Bash``-scoped action gate (e.g.
        a ``gh pr create`` / ``git push`` via ``execute_shell_command``); the
        built-in ``Agent`` tool stays denied as today. Returns ``None`` when the
        server is off, so callers pass it straight through to ``QueryOptions`` and
        the default ``["Agent"]`` denial is preserved byte-for-byte.
        """
        if not self.serena_mcp_enabled:
            return None
        return ["Agent", *(f"mcp__serena__{tool}" for tool in SERENA_MUTATING_TOOLS)]

    def check_mcp_launchers(
        self, servers: dict[str, dict[str, Any]] | None = None
    ) -> list[str]:
        """Validate that every enabled external MCP server's launcher is on PATH.

        Each external MCP server (``context7`` / ``serena``) is
        started as a child process by a launcher binary — ``npx`` or ``uvx`` —
        that is not provisioned in the committed production image.
        When an enabled server's launcher is missing from ``PATH`` the failure
        otherwise surfaces only at the first query as a non-retried ``CLI_CRASH``
        that crashes the loop mid-iteration. This check resolves each launcher
        with :func:`shutil.which` — mirroring the ``shutil.which("claude")``
        precedent in :meth:`check_secrets` — and returns one actionable message
        per enabled server whose launcher is absent so a caller can fail loud
        and early instead. The remediation hint names the toolchain that ships
        each launcher (``uv`` for ``uvx``, Node.js for ``npx`` via
        :data:`MCP_LAUNCHER_INSTALL_HINTS`), not the launcher binary itself.

        *servers* lets a caller that already built the spec dict pass it in to
        avoid a second :meth:`mcp_servers_config` call (and its env re-read);
        when ``None`` (the default — the standalone ``preflight`` path) the dict
        is built here.

        Returns an empty list when every enabled launcher resolves or no
        external server is enabled — a no-op in every environment where the
        relevant ``[mcp]`` flag is off. Only inspects the external subprocess
        specs from :meth:`mcp_servers_config`; in-process MCP servers (email,
        telegram, introspection) are launcher-less and never appear here.
        """
        if servers is None:
            servers = self.mcp_servers_config()
        if not servers:
            return []
        errors: list[str] = []
        for name, spec in servers.items():
            command = spec.get("command")
            if command and shutil.which(command) is None:
                hint = MCP_LAUNCHER_INSTALL_HINTS.get(command, f"install {command}")
                errors.append(
                    f"{name} MCP server is enabled "
                    f"([mcp] {name}_enabled = true) but its launcher "
                    f"{command!r} was not found on PATH — {hint} or "
                    f"set [mcp] {name}_enabled = false"
                )
        return errors

    @property
    def artifacts_dir(self) -> Path:
        """Root directory for task execution artifacts."""
        return self.workspace / "artifacts"

    @property
    def pid_path(self) -> Path:
        """Path to the PID file for the running Maestro process."""
        return self.memory_dir / "maestro.pid"

    @property
    def log_dir(self) -> Path:
        """Root directory for all log files."""
        return self.memory_dir / "logs"

    @property
    def app_log_path(self) -> Path:
        """Path to the application log file."""
        return self.log_dir / "maestro.log"

    @property
    def sdk_log_dir(self) -> Path:
        """Directory for SDK session logs, organized by date."""
        return self.log_dir / "sdk"

    @property
    def cost_samples_path(self) -> Path:
        """Path to the append-only cost samples JSONL file."""
        return self.memory_dir / "cost_samples.jsonl"

    @property
    def usage_log_path(self) -> Path:
        """Path to the per-iteration usage JSONL file."""
        return self.memory_dir / "usage.jsonl"

    @property
    def gate_runs_path(self) -> Path:
        """Path to the append-only gate-run telemetry JSONL file.

        The host execution lease (proposal 126) appends one
        :class:`~maestro.gate_capacity.GateRunRecord` per admitted gate run
        here. It lives under the per-checkout ``memory_dir`` with the other
        telemetry ledgers — deliberately *not* under the host-shared
        ``gate_lease_slot_dir``, which scopes the concurrency slots across
        checkouts rather than the trace.
        """
        return self.memory_dir / "gate_runs.jsonl"

    @property
    def gate_environment_path(self) -> Path:
        """Path to the cached gate-runnability verdict row.

        The single JSON row
        :func:`maestro.gate_environment.refresh_verdict` writes and
        :func:`maestro.gate_environment.read_verdict` reads. A single-row
        sidecar rather than an append-only ledger because only the *current*
        environment is decidable: a verdict from two iterations ago says
        nothing about whether the gate can answer now. Sits beside
        ``gate_runs.jsonl`` under the per-checkout ``memory_dir``, since
        provisioning and fit are facts about *this* checkout's environment.
        """
        return self.memory_dir / "gate_environment.json"

    @property
    def gate_environment_notice_path(self) -> Path:
        """Path to the ask-once notice row for the gate-runnability verdict.

        The single JSON row :func:`maestro.gate_environment.record_notice`
        writes and :func:`maestro.gate_environment.read_notice` reads: the
        ``(kind, identities)`` key of the last environment fact Thomas was asked
        about. A sidecar beside ``gate_environment.json`` rather than a field on
        it because the two have different lifetimes — the verdict row is
        rewritten on every iteration whose probe runs, while the notice must
        survive exactly as long as the fact does, so a fingerprint that moves
        under a standing fact re-stamps the held plans without re-asking.
        """
        return self.memory_dir / "gate_environment_notice.json"

    @property
    def task_history_path(self) -> Path:
        """Path to the append-only task-history JSONL file.

        Mirrors :class:`~maestro.memory.Memory`'s own ``task_history.jsonl``
        root so read-side tooling (e.g. the ``burn-baseline`` report) can
        resolve the corpus without instantiating ``Memory``.
        """
        return self.memory_dir / "task_history.jsonl"

    @property
    def dashboard_pid_path(self) -> Path:
        """Path to the PID file for the running dashboard process."""
        return self.memory_dir / "maestro-dashboard.pid"

    @property
    def lifecycle_path(self) -> Path:
        """Path to the lifecycle state file (shutdown/startup tracking)."""
        return self.memory_dir / "lifecycle.json"

    @property
    def maintenance_state_path(self) -> Path:
        """Sidecar for the operator-initiated maintenance-mode flag.

        Separated from ``lifecycle.json`` so the Telegram command handler
        can mutate the flag without racing the loop's lifecycle writer or
        capturing stale mid-iteration ephemeral fields.
        """
        return self.memory_dir / "maintenance.json"

    @property
    def progress_aware_lap_budget_active(self) -> bool:
        """Whether the progress-exhaustion-gated merge-or-close handoff is live.

        The single owner of "is the progress-aware lap budget active?" — the
        union of the dedicated ``progress_aware_lap_budget_enabled`` flag and the
        salvage master flag ``salvage_before_disengage_enabled`` (proposal 094).
        Every handoff site (the task-generation gate, the Tend route) and the
        per-lap readiness snapshot consult this one predicate, so flipping the
        salvage master flag activates the progress-aware handoff everywhere at
        once and the consuming sites can never drift apart. Pure/derived; ``False``
        while both flags are off, so production behavior is unchanged.
        """
        return (
            self.progress_aware_lap_budget_enabled
            or self.salvage_before_disengage_enabled
        )

    @property
    def active_refutation_release_cap(self) -> int:
        """The refuted-release budget in force, or ``0`` when the path is off.

        The single owner of "how large is the refuted-release budget right now?"
        — the union of :attr:`standdown_refutation_release_cap` and the
        ``refutable_standdown_enabled`` master flag (proposal 135). Every
        ``deliver_handoff`` site stamps ``StanddownRecord.refutation_releases``
        from this one value, so the inspection projection and the release path
        it projects can never disagree about whether the budget is live. Pure and
        derived; ``0`` while the flag is off, so ``deliver_handoff`` skips the
        probe entirely and the default-off path stays byte-identical.
        """
        if not self.refutable_standdown_enabled:
            return 0
        return self.standdown_refutation_release_cap

    @property
    def gate_lease_dir(self) -> Path | None:
        """The gate execution lease's slot directory when active, else ``None`` (proposal 126).

        The single derived reader every in-process gate site consults: returns
        ``gate_lease_slot_dir`` when ``gate_lease_enabled`` is on and ``None`` when
        it is off, so ``gate_capacity.lease_for_gate_run`` receives one ``Path |
        None`` carrying both the flag and the directory. Every routed reader
        (``run_quality_gates``, ``run_gate_check``, ``preflight``, and the
        ``cmd_gate_lease`` wrapper) passes this property and none restates the flag
        read — mirroring the derived-flag-property precedent of
        ``progress_aware_lap_budget_active``. Pure/derived; ``None`` while the flag
        is off, so production behaviour is unchanged.
        """
        return self.gate_lease_slot_dir if self.gate_lease_enabled else None

    def resolved_sdk_provider(self) -> str:
        """Return the active provider name (proposal 058).

        Returns ``sdk_provider`` directly — the proposal-056 ``[sdk] backend``
        alias was retired in 056 Step 11. The name returned is the *schema* name;
        ``configure_provider`` re-checks it against the registry at startup and
        fails loud for not-yet-registered names.
        """
        return self.sdk_provider

    def work_target(self) -> WorkTarget:
        """Assemble the loop's :class:`~maestro.work_target.WorkTarget` (proposal 107).

        The sole assembly point for the four work facets, analogous to
        ``resolved_sdk_provider()``. Every facet defaults to today's self values:
        ``repo_workspace`` / ``github_repo`` fall back to ``workspace`` /
        ``github_repo`` when their ``[work]`` override is empty; the gate path
        atoms (source, tests, and the lint-only extras) and the two framings are
        passed through verbatim (a retarget of source/tests must also override
        ``gate_lint_extra_paths``, which is not derived from them). The framings are
        ``cast`` — ``validate()`` already guaranteed they are members of
        ``VALID_WORK_FRAMINGS``. The two gate-path atoms are consumed by the
        four quality-gate readers and the two repo-location facets by the
        loop-facing git / GitHub client construction seams; the focus dial by
        ``phase_will_run`` and the framing by the ``prompts`` selectors.
        """
        repo_workspace = (
            Path(self.work_repo_workspace).expanduser()
            if self.work_repo_workspace
            else self.workspace
        )
        return WorkTarget(
            repo_workspace=repo_workspace,
            github_repo=self.work_github_repo or self.github_repo,
            gate_source_path=self.work_gate_source_path,
            gate_tests_path=self.work_gate_tests_path,
            gate_lint_extra_paths=tuple(self.work_gate_lint_extra_paths),
            task_framing=cast(WorkFraming, self.work_task_framing),
            focus_profile=cast(WorkFraming, self.work_focus_profile),
        )

    def provider_for_phase(self, phase: str) -> str:
        """Return the provider name for *phase*, honoring per-phase overrides.

        Falls back to ``resolved_sdk_provider()`` for phases without an explicit
        ``[sdk] <phase>_provider`` override. This is the Step-6 resolver; Step 7
        threads it into the phase call sites.
        """
        return self.sdk_phase_providers.get(phase, self.resolved_sdk_provider())

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = ".env",
        config_dir: Path | None = None,
    ) -> Config:
        # Secrets are read from ``os.environ`` inside the declarative builder, so
        # ``.env`` must load first. ``build_config_authoritative`` (proposal 106)
        # is the sole parser: it merges base + ``{env}`` overlay, validates via the
        # section models plus the shared ``_check_cross_field_and_subtable`` guard
        # the declarative build runs (the legacy ``Config.validate`` raise-ladder is
        # no longer on the load path), and normalises
        # ``pydantic.ValidationError`` -> ``ValueError`` — preserving the
        # "``from_env`` raises ``ValueError`` on bad config" contract.
        if env_file is not None:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)

        from maestro import config_settings

        toml_dir = config_dir or Path.cwd()
        env_name = os.environ.get("MAESTRO_ENV")
        return config_settings.build_config_authoritative(toml_dir, env_name)

    def ensure_dirs(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.sdk_log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "Cannot create %s — SDK logging unavailable until "
                "host-side permissions are fixed",
                self.sdk_log_dir,
            )

    @staticmethod
    def detect_auth() -> str | None:
        """Return ``"oauth"``, ``"api_key"`` or ``None`` — :meth:`check_auth` without raising."""
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "oauth"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "api_key"
        return None

    @staticmethod
    def detect_billing_auth() -> str | None:
        """Return the auth method that *bills* the call, for the cost stamp.

        Claude Code's documented credential precedence puts
        ``ANTHROPIC_API_KEY`` ahead of ``CLAUDE_CODE_OAUTH_TOKEN``, so when
        both are set the call is metered against the key even though
        :meth:`detect_auth` reports ``oauth``. Stamping ``api_key`` then keeps
        the reported cash cost from under-reporting; a WARNING names the clash.
        """
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if has_key and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            logger.warning(
                "Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are set; "
                "the Claude CLI bills the API key, so cost samples are "
                "stamped metered"
            )
        if has_key:
            return "api_key"
        return Config.detect_auth()

    @staticmethod
    def check_auth() -> str:
        """Check which authentication method is configured.

        Returns ``"oauth"`` or ``"api_key"`` depending on the active credential.
        Raises :class:`SystemExit` with setup instructions when neither is set.
        """
        method = Config.detect_auth()
        if method is not None:
            return method
        raise SystemExit(
            "No Claude authentication found.\n\n"
            "Set one of the following environment variables (or add to .env):\n\n"
            "  Option 1 — API key (pay-per-token):\n"
            "    ANTHROPIC_API_KEY=sk-ant-api03-...\n"
            "    Get one at https://console.anthropic.com\n\n"
            "  Option 2 — OAuth token (Max/Pro subscription, personal use):\n"
            "    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...\n"
            "    Generate with: npm install -g @anthropic-ai/claude-code && claude setup-token"
        )

    @staticmethod
    def auth_label(auth_method: str | None = None, *, provider: str = "anthropic") -> str:
        """Human-readable label for the active auth method.

        When *auth_method* is one of the known values (``"oauth"`` /
        ``"api_key"``) it is reused directly, letting a caller that already
        resolved the method via :meth:`check_auth` skip a second redundant
        env read (#356). ``None`` or any unexpected value falls through to
        :meth:`check_auth`, so a malformed passthrough re-resolves rather
        than silently mislabelling.

        *provider* names the active ``[sdk] provider``. Only ``anthropic``
        authenticates through :meth:`check_auth`; for any other provider the
        anthropic auth env is irrelevant, so the label is derived from the
        provider name **without** resolving (or requiring) anthropic
        credentials. This keeps a correctly-configured non-anthropic
        deployment — its own key present, anthropic creds absent — from
        crashing at the label step with the misleading ``No Claude
        authentication found`` error (#356 review); the active provider's own
        credential is already validated by :meth:`check_secrets` upstream.
        """
        if provider != "anthropic":
            return f"{provider} provider credential"
        method = auth_method if auth_method in ("oauth", "api_key") else Config.check_auth()
        if method == "oauth":
            return "OAuth token (Max/Pro subscription)"
        return "API key"

    @staticmethod
    def _claude_cli_logged_in() -> bool:
        """Best-effort, non-mutating probe of the ``claude`` CLI login state.

        Runs ``claude auth status --json`` — a read-only command that prints
        ``{"loggedIn": true, ...}`` for an authenticated install — with a
        short timeout. Returns ``True`` only when the probe exits cleanly
        and reports ``loggedIn`` true; every failure mode (non-zero exit,
        timeout, missing binary, unparseable output, or ``loggedIn``
        false/absent) returns ``False`` so the caller raises the
        ``claude auth login`` remediation. Proposal 058 Step 8 shipped only
        the PATH check and deferred this login probe to proposal 056 Step 13.

        Catching the specific subprocess/OS errors converts a probe failure
        into the documented "not logged in" verdict (the caller then fails
        loud); it is not a defect-silencing handler.
        """
        try:
            result = subprocess.run(
                ["claude", "auth", "status", "--json"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("loggedIn") is True

    @staticmethod
    def _check_provider_credential(
        provider: str, auth_method: str | None = None,
    ) -> list[str]:
        """Validate the LLM-provider credential for *provider* (proposal 058).

        Returns warning strings for a present-but-malformed credential; raises
        :class:`SystemExit` with a provider-specific remediation when the
        required credential is absent. Dispatches on the resolved
        ``[sdk] provider`` name (``anthropic`` / ``openai`` / ``cli_paced``).
        Under the default ``anthropic`` this reproduces the prior
        :meth:`check_auth` + prefix-validation behaviour exactly. The
        ``cli_paced`` branch requires the ``claude`` CLI on PATH **and** an
        active login (a ``claude auth status`` dry-probe via
        :meth:`_claude_cli_logged_in`; proposal 056 Step 13).

        *auth_method*, when one of the known values (``"oauth"`` /
        ``"api_key"``), is reused for the ``anthropic`` branch instead of
        calling :meth:`check_auth` — the caller already resolved it and the
        second read is redundant (#356/#357). The missing-credential
        :class:`SystemExit` guard then lives at that single resolution point.
        ``None`` or any unexpected value falls through to :meth:`check_auth`,
        preserving every existing caller (and failing loud on a malformed
        passthrough rather than mislabelling).
        """
        warnings: list[str] = []
        if provider == "anthropic":
            # raises SystemExit if missing (only when not reusing a method)
            auth = (
                auth_method
                if auth_method in ("oauth", "api_key")
                else Config.check_auth()
            )
            # Format validation for whichever credential is active
            if auth == "oauth":
                token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
                if not token.startswith("sk-ant-oat"):
                    warnings.append(
                        "CLAUDE_CODE_OAUTH_TOKEN has unexpected prefix "
                        "(expected 'sk-ant-oat...')"
                    )
            else:
                key = os.environ.get("ANTHROPIC_API_KEY", "")
                if not key.startswith("sk-ant-api"):
                    warnings.append(
                        "ANTHROPIC_API_KEY has unexpected prefix "
                        "(expected 'sk-ant-api...')"
                    )
        elif provider == "openai":
            key = os.environ.get("OPENAI_API_KEY", "")
            if not key:
                raise SystemExit(
                    "OPENAI_API_KEY is not set.\n\n"
                    'The "openai" SDK provider ([sdk] provider = "openai") '
                    "requires an OpenAI API key.\n"
                    "Create one at https://platform.openai.com/api-keys\n\n"
                    "Then set:  OPENAI_API_KEY=sk-... (or add to .env)"
                )
            if not key.startswith("sk-"):
                warnings.append(
                    "OPENAI_API_KEY has unexpected prefix (expected 'sk-...')"
                )
        elif provider == "cli_paced":
            if shutil.which("claude") is None:
                raise SystemExit(
                    'The "cli_paced" SDK provider '
                    '([sdk] provider = "cli_paced") requires the `claude` CLI '
                    "on PATH and an active login.\n\n"
                    "Install:  npm install -g @anthropic-ai/claude-code\n"
                    "Log in:   claude auth login\n\n"
                    "See proposal 056 (CLI-Paced SDK Backend)."
                )
            if not Config._claude_cli_logged_in():
                raise SystemExit(
                    'The "cli_paced" SDK provider '
                    '([sdk] provider = "cli_paced") found the `claude` CLI on '
                    "PATH but it is not logged in.\n\n"
                    "Log in:   claude auth login\n\n"
                    "See proposal 056 (CLI-Paced SDK Backend)."
                )
        else:
            raise SystemExit(
                f"Unknown SDK provider {provider!r}; expected one of "
                f"{sorted(VALID_SDK_PROVIDERS)}."
            )
        return warnings

    @staticmethod
    def _check_phase_provider_credentials(
        global_provider: str, phase_providers: dict[str, str],
    ) -> list[str]:
        """Phase-aware credential checks for per-phase provider routes (067 Step 1).

        The global-provider check (:meth:`_check_provider_credential`) never
        sees ``[sdk] <phase>_provider`` overrides, so before this gate a
        per-phase ``cli_paced`` route had no login precondition at all. For
        every provider named in *phase_providers* (skipping *global_provider*,
        already checked), the verdict depends on where it is routed:

        * Routed to any phase in :data:`MUTATING_SDK_PHASE_NAMES` — a phase
          that must be able to execute — a missing credential raises
          :class:`SystemExit`, re-wrapped to name the phase route while
          keeping the provider remediation (e.g. ``claude auth login``).
        * Routed only to non-mutating phases — the same missing credential
          degrades to a returned warning: the loop can start, those phases
          fail loud at their first query. The narrow ``except SystemExit`` is
          the documented downgrade contract, not a silenced defect.

        Returns warning strings (malformed-credential warnings plus any
        downgraded verdicts).
        """
        warnings: list[str] = []
        phases_by_provider: dict[str, list[str]] = {}
        for phase, name in sorted(phase_providers.items()):
            if name == global_provider:
                continue
            phases_by_provider.setdefault(name, []).append(phase)
        for name, phases in sorted(phases_by_provider.items()):
            mutating = sorted(set(phases) & MUTATING_SDK_PHASE_NAMES)
            if mutating:
                try:
                    warnings.extend(Config._check_provider_credential(name))
                except SystemExit as exc:
                    raise SystemExit(
                        f"provider {name!r} is routed to phase(s) "
                        f"{', '.join(mutating)} via [sdk] <phase>_provider — "
                        f"phases that must be able to execute — but its "
                        f"credential check failed:\n\n{exc}"
                    ) from exc
            else:
                try:
                    warnings.extend(Config._check_provider_credential(name))
                except SystemExit as exc:
                    warnings.append(
                        f"provider {name!r} routed to non-mutating phase(s) "
                        f"{', '.join(phases)} has no usable credential — "
                        f"those phases will fail at their first query: {exc}"
                    )
        return warnings

    @staticmethod
    def check_secrets(
        publication_enabled: bool = False,
        *,
        provider: str = "anthropic",
        phase_providers: dict[str, str] | None = None,
        check_publication_scope: bool = False,
        smoke: bool = False,
        auth_method: str | None = None,
    ) -> list[str]:
        """Validate that secret env vars are present and well-formed.

        Dispatches the LLM-provider credential precondition on *provider*
        (proposal 058 Step 8) via :meth:`_check_provider_credential`:
        ``anthropic`` requires ``ANTHROPIC_API_KEY`` or
        ``CLAUDE_CODE_OAUTH_TOKEN``; ``openai`` requires ``OPENAI_API_KEY``;
        ``cli_paced`` requires the ``claude`` CLI on PATH **and** an active
        login (a non-mutating ``claude auth status`` dry-probe, proposal 056
        Step 13). Each missing precondition raises :class:`SystemExit` with a
        provider-specific remediation message. *phase_providers* — the
        ``[sdk] <phase>_provider`` routing map — extends the same check to
        per-phase providers with a phase-aware verdict
        (:meth:`_check_phase_provider_credentials`, proposal 067 Step 1):
        hard :class:`SystemExit` when routed to a phase in
        :data:`MUTATING_SDK_PHASE_NAMES`, warning otherwise. ``None`` (the
        default) checks only the global provider, preserving the prior
        behaviour for callers that route nothing per-phase. Then checks
        ``GITHUB_TOKEN`` (required, raises :class:`SystemExit` if missing) —
        provider-neutral, as are the optional Telegram/dashboard/publication
        checks below.

        *auth_method* — the already-resolved ``check_auth`` verdict — is passed
        through to :meth:`_check_provider_credential` so the ``anthropic``
        branch reuses it instead of re-reading the auth env vars (#356/#357); it
        applies only to the global-provider check, never the per-phase
        providers (which resolve their own credentials). ``None`` (the default)
        preserves the prior self-resolving behaviour for every existing caller.

        The proposal-039 ``gh auth status --show-token`` probe runs only when
        all three of ``publication_enabled``, ``not smoke``, and
        ``check_publication_scope`` are true. ``check_publication_scope``
        defaults to False so short-lived CLI callers (``cmd_run``,
        ``cmd_contemplate``, ``cmd_evolve``, ``cmd_serve``) do not pay the
        subprocess cost or the 5-second ``gh``-not-on-PATH timeout; only
        ``cmd_loop`` and ``cmd_preflight`` opt in to the probe by passing
        True. The probe itself remains non-blocking and swallows any error.

        Returns a list of warning strings for optional or malformed secrets.
        An empty list means all secrets are healthy.
        """
        warnings: list[str] = []

        # --- Required: provider credential (proposal 058 dispatch) ---
        warnings.extend(
            Config._check_provider_credential(provider, auth_method=auth_method)
        )

        # --- Per-phase provider credentials, phase-aware (proposal 067) ---
        if phase_providers:
            warnings.extend(
                Config._check_phase_provider_credentials(provider, phase_providers)
            )

        # --- Required: GitHub token ---
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        if not gh_token:
            raise SystemExit(
                "GITHUB_TOKEN is not set.\n\n"
                "A GitHub personal access token is required for evolution PRs "
                "and issue tracking.\n"
                "Create one at https://github.com/settings/tokens with scopes:\n"
                "  contents:write, pull_requests:write, issues:write\n\n"
                "Then set:  GITHUB_TOKEN=ghp_... (or add to .env)"
            )
        _gh_prefixes = ("ghp_", "gho_", "github_pat_", "ghs_", "ghu_")
        if not gh_token.startswith(_gh_prefixes):
            warnings.append(
                f"GITHUB_TOKEN has unexpected prefix "
                f"(expected one of {', '.join(_gh_prefixes)})"
            )

        # --- Optional: Telegram bot token ---
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not tg_token:
            warnings.append(
                "TELEGRAM_BOT_TOKEN not set — Telegram notifications disabled"
            )
        elif not re.match(r"^\d+:[A-Za-z0-9_-]+$", tg_token):
            warnings.append(
                "TELEGRAM_BOT_TOKEN has unexpected format "
                "(expected '<digits>:<alphanumeric>')"
            )

        # --- Optional: Telegram chat ID ---
        if not os.environ.get("TELEGRAM_CHAT_ID"):
            warnings.append(
                "TELEGRAM_CHAT_ID not set — Telegram notifications disabled"
            )

        # --- Optional: Dashboard token ---
        if not os.environ.get("MAESTRO_DASHBOARD_TOKEN"):
            warnings.append(
                "MAESTRO_DASHBOARD_TOKEN not set — dashboard API is unauthenticated"
            )

        # --- Optional: GITHUB_TOKEN scope check (proposal 039) ---
        # Best-effort warning when publication is enabled but the deploy PAT
        # lacks the `repo` scope that `gh repo create` needs. Non-blocking,
        # swallows on any failure, skipped in smoke mode.
        if publication_enabled and gh_token and not smoke and check_publication_scope:
            try:
                result = subprocess.run(
                    ["gh", "auth", "status", "--show-token"],
                    capture_output=True, text=True, timeout=5,
                )
                if not re.search(r"\brepo(?![\w:])", result.stderr + result.stdout):
                    logger.warning(
                        "publication_enabled=true but GITHUB_TOKEN does not "
                        "appear to carry the 'repo' scope. gh repo create will "
                        "fail. See proposal 039."
                    )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass  # best-effort; gh missing / timed out / OS-side error.

        return warnings
