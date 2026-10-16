"""Experimental-flag lifecycle — a dated promote-or-delete forcing function.

``Config`` carries hundreds of dataclass fields, a large fraction of them opt-in,
default-off "experimental" feature switches accreted by the self-evolution track
— one flag per gate, bridge, or trial behaviour. Two static CI guards already keep
that flat surface honest (see ``tests/test_config_flag_lifecycle.py``): one fails
when a field has no reader anywhere under ``maestro/``, and one binds the TOML
mapping table to the committed ``maestro.toml`` defaults. What was still missing is
any mechanism to *retire* an experiment: a default-off flag that proved out (or
didn't) otherwise leaves its switch behind forever, with no provenance and no
"review by" horizon.

This module is that mechanism — a lightweight, **advisory** manifest. Each tracked
flag declares a ``review_by`` date and (where the gated behaviour emits a gate
activation) the ``gate_activations.jsonl`` gate name to count. A pure CI guard
fails once any manifest entry passes its review horizon — the forcing function that
turns a silent, immortal experiment into a dated promote-or-delete decision. The
read-only ``maestro audit-flags`` command joins the manifest against recent
gate-activation counts so the operator has the evidence in one place. Nothing is
auto-deleted: the manifest reports, the human decides — consistent with the
human-in-the-loop evolution invariant.

A second, orthogonal dimension is the ``maestro.{env}.toml`` **overlay** vs. base,
and it is **symmetric** — the overlay both enables flags base keeps off and disables
flags base keeps on. Three manifests declare that diverged surface so CI guards can
make it honest. On the additive side (``true`` in the overlay only):
:data:`PROD_ONLY_CANARIES` (time-boxed, dated horizon, *meant* to converge) and
:data:`PROD_ONLY_OPERATIONAL` (permanent operational/topology divergences with no
horizon). On the subtractive side (``true`` in base, ``false`` in the overlay):
:data:`PROD_ONLY_DISABLED` — production dormancy levers and kill-switches, reusing
:class:`EnvOverlayCanary` because they too carry a dated horizon and must converge.
:func:`enabled_only_in_overlay` and :func:`disabled_only_in_overlay` are the pure
prod-vs-base diffs the guards compute the real surfaces with, each self-cleaning in
both directions. These are the overlay-axis analogue of the base-default manifests
above.

Every helper here is pure and clock-free — ``today`` is always a parameter, never a
wall-clock read — so the lifecycle logic is deterministically testable. Only the
``audit-flags`` CLI command reads ``date.today()``; the CI staleness guards judge
horizons against :data:`FREEZE_DATE`.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Protocol


@dataclass(frozen=True)
class ExperimentalFlag:
    """One tracked opt-in ``Config`` flag with a review horizon.

    Kept a dataclass (not a bare ``{flag: date}`` dict) so the entry can grow
    later — a cut-over lifecycle stage, an off-path symbol — without reshaping the
    manifest.
    """

    #: A real ``Config`` field name (the manifest keys on the field name, not the
    #: TOML key — the two diverge for some flags).
    flag: str
    #: ISO ``YYYY-MM-DD`` promote-or-delete horizon; a crossed date reds the build.
    review_by: str
    #: The ``gate_activations.jsonl`` gate name whose fire-rate to count, or
    #: ``None`` when the gated behaviour emits no activation.
    gate: str | None
    #: One line on what the flag gates — provenance the switch itself never carried.
    note: str


@dataclass(frozen=True)
class CutoverFlag:
    """One default-on cut-over ``Config`` flag and its collapse-lifecycle stage.

    A cut-over flag is the opposite animal to an :class:`ExperimentalFlag`: it is
    default-*on*, and its flag-*off* branch is live *old* logic (or a deliberately
    retained fallback). It therefore has readers — the dead-flag reader guard
    passes — and is not an experiment, so it never enters :data:`EXPERIMENTAL_FLAGS`.
    Kept a distinct record (not merged into ``ExperimentalFlag``) because the two
    categories are semantically opposite: a default-off experiment versus a
    default-on cut-over with a live off-path. The ``stage`` names where the flag
    sits on its collapse path; :func:`find_stage_violations` binds the stage to
    whether ``off_path_symbol`` still exists so a half-done or leftover collapse
    reds the build. The optional ``gate`` names the ``gate_activations.jsonl``
    gate whose fire-rate measures the flag's off-path being exercised (a
    divergence), read by :func:`cutover_burn_in_report`; ``None`` when no
    divergence gate is wired, so the flag stays unmeasured.
    """

    #: A real ``Config`` field name (the manifest keys on the field name, not the
    #: TOML key — the two diverge for some flags).
    flag: str
    #: The proposal that flipped the flag on, or ``None`` for a flag that predates
    #: proposal numbering (mirroring how ``ExperimentalFlag.gate`` is ``None`` when
    #: no gate applies).
    proposal: int | None
    #: ISO ``YYYY-MM-DD`` flip-on date — the burn-in-age anchor a later report reads.
    cutover_cycle: str
    #: The ``maestro/``-defined symbol whose deletion marks the collapse — or, for a
    #: retained fallback, the permanent symbol that keeps the off-path wired.
    off_path_symbol: str
    #: ISO ``YYYY-MM-DD`` collapse-decision horizon, parseable by
    #: :func:`parse_review_by`.
    review_by: str
    #: Where the flag sits on its collapse path. ``collapsed`` is transient — a
    #: completed collapse removes the entry rather than persisting the marker — so a
    #: lingering ``collapsed`` entry is always a violation.
    stage: Literal["cutover", "collapse_eligible", "collapsed", "retain"]
    #: One line of provenance the switch itself never carried.
    note: str
    #: The ``gate_activations.jsonl`` gate name whose fire-rate measures the flag's
    #: off-path being exercised (a divergence), mirroring ``ExperimentalFlag.gate``;
    #: ``None`` when no divergence gate is wired (the burn-in stays unmeasured).
    #: Appended last with a default so existing manifest entries need no change.
    gate: str | None = None


@dataclass(frozen=True)
class EnvOverlayCanary:
    """One time-boxed prod-only-diverged flag with a dated graduation horizon.

    The overlay-axis analogue of :class:`ExperimentalFlag`, along a *different*
    dimension: an :class:`ExperimentalFlag` tracks a *base default-off* flag's
    promote-or-delete horizon, whereas this tracks a flag whose value in
    ``maestro.{env}.toml`` **diverges from base** — and therefore from every other
    environment — in either direction. A canary is *meant* to converge: a proposal
    diverged it in production to gather evidence under a documented graduation
    criterion, so its ``review_by`` reds the build once the observation window
    lapses and forces a human to graduate it (promote the value to base) or revert
    it. Kept distinct from
    :class:`EnvOverlayDivergence` for the same reason ``CutoverFlag`` is distinct
    from ``ExperimentalFlag`` — the two overlay records govern semantically opposite
    lifecycles: this one must converge, that one must never.

    The record carries **either** overlay polarity. :data:`PROD_ONLY_CANARIES` holds
    the additive direction (off in base, turned **on** in the overlay);
    :data:`PROD_ONLY_DISABLED` holds the subtractive one (on in base, turned **off**
    in the overlay — a burn-in dormancy lever or a production kill-switch). The
    ``review_by`` / ``note`` / ``justification`` contract and :func:`parse_review_by`
    are identical in both directions, so the manifest a record sits in — not a field
    on the record — names its polarity, and the two manifests are disjoint by
    construction (a flag cannot be prod-enabled and prod-disabled at once).
    """

    #: A ``Config`` field name — the key :func:`enabled_only_in_overlay` (additive
    #: manifest) or :func:`disabled_only_in_overlay` (subtractive manifest)
    #: compares, **not** the TOML key (the two diverge for several flags, e.g.
    #: ``[mcp] context7_enabled`` maps to ``context7_mcp_enabled``), so the
    #: set-equality guards can compare directly.
    flag: str
    #: The overlay environment the flag diverges in (``"production"`` today).
    env: str
    #: ISO ``YYYY-MM-DD`` graduation horizon; a crossed date reds the build,
    #: parseable by :func:`parse_review_by`.
    review_by: str
    #: One line of provenance: the proposal, the criteria doc, and the deploy date.
    note: str
    #: The explicit observability/operational reason this flag is a prod-only
    #: divergence rather than a base default flipped everywhere. **Required** for any entry
    #: added after the baseline freeze (:data:`PROD_ONLY_CANARY_BASELINE`) — a bare
    #: new canary reds :func:`find_unjustified_canaries`'s guard; **empty** for the
    #: grandfathered baseline entries, which predate the discipline. Appended last
    #: with a default so the existing baseline entries need no change (the
    #: ``CutoverFlag.gate`` idiom).
    justification: str = ""


@dataclass(frozen=True)
class EnvOverlayDivergence:
    """One permanent prod-only-enabled flag — an operational/topology divergence.

    Production-only **by design**, with **no** ``review_by`` and **no** graduation
    criterion, because it must never converge to base: dev/local have no equivalent
    (no reverse proxy, no peak-hour operational choice, no autonomous curation
    phase), so forcing a horizon on it would be dishonest — the overdue guard would
    eventually red the build demanding a human "graduate" a setting that must stay
    diverged forever. It is therefore recorded (visible, provenance-carrying) but
    never overdue-checked — the opposite lifecycle to :class:`EnvOverlayCanary`.
    """

    #: A flattened ``Config`` field name — see :attr:`EnvOverlayCanary.flag`.
    flag: str
    #: The overlay environment the flag is enabled in (``"production"`` today).
    env: str
    #: One line of provenance: why the divergence is permanent, plus its enable
    #: date/SHA. No ``review_by`` — a permanent divergence is never overdue-checked.
    note: str


#: The repo is frozen, so CI judges flag horizons as of the freeze, not the wall clock.
FREEZE_DATE: date = date(2026, 10, 15)


#: Curated starter set of genuinely opt-in, default-off ``Config`` flags. Fifteen
#: carry a gate trace (a measurable fire-rate); thirty-two do not. ``review_by`` dates
#: sit comfortably in the future — a crossed horizon is the intended red-build
#: forcing function, not a same-week emergency.
EXPERIMENTAL_FLAGS: tuple[ExperimentalFlag, ...] = (
    ExperimentalFlag(
        flag="refutable_standdown_enabled",
        review_by="2027-09-30",
        gate="standdown_refuted",
        note="third standdown release path — drops a handoff-ready latch the "
        "live owed-act reading refutes, off facts the tend walk already measured.",
    ),
    ExperimentalFlag(
        flag="owned_act_preempt_enabled",
        review_by="2027-05-31",
        gate=None,
        note="redirects a stalled remediation slot to an owned mechanical prune "
        "under the rule-18 drought.",
    ),
    ExperimentalFlag(
        flag="ci_pending_green_handoff_enabled",
        review_by="2027-09-30",
        gate="ci_pending_green_handoff",
        note="hands a locally-green PR up at the CI-pending hold's fire point "
        "instead of re-yielding its slot in silence.",
    ),
    ExperimentalFlag(
        flag="sdk_single_use_evolution_calls_enabled",
        review_by="2027-09-30",
        gate=None,
        note="runs each evolution-lineage call on its own single-use session at "
        "its registry effort instead of the shared slot-B session.",
    ),
    ExperimentalFlag(
        flag="self_review_own_session_enabled",
        review_by="2027-09-30",
        gate=None,
        note="runs each self-review round on its own single-use session at "
        "the registry's high effort instead of a turn on the implementer's.",
    ),
    ExperimentalFlag(
        flag="sdk_call_policy_resolution_enabled",
        review_by="2027-09-30",
        gate=None,
        note="resolves every SDK call's effort from the call-policy registry "
        "at the seam and refuses a session turn at a mismatched effort.",
    ),
    ExperimentalFlag(
        flag="sdk_enforced_tool_surface_enabled",
        review_by="2027-09-30",
        gate=None,
        note="derives every SDK call's tools from its caller's declared tool "
        "surface at the seam and refuses a call or session turn off-surface.",
    ),
    ExperimentalFlag(
        flag="session_landing_enabled",
        review_by="2027-05-31",
        gate=None,
        note="post-session mechanical commit+push of gate-clean owned-branch edits "
        "before verdict composition.",
    ),
    ExperimentalFlag(
        flag="obligation_fork_floor_enabled",
        review_by="2027-06-30",
        gate=None,
        note="wires the obligation-fork verdict floor into run().",
    ),
    ExperimentalFlag(
        flag="mergeable_merge_request_enabled",
        review_by="2027-06-30",
        gate="bind_mergeable_flip_to_merge_request",
        note="binds a mergeable-flip observation to a merge request.",
    ),
    ExperimentalFlag(
        flag="phantom_premise_gate_enabled",
        review_by="2027-09-30",
        gate="phantom_premise",
        note="refuses a generated goal claiming an existing defect in a .py "
        "path absent from the tree, admitting a goal framed as creating that path.",
    ),
    ExperimentalFlag(
        flag="frame_discharge_bind_enabled",
        review_by="2027-08-31",
        gate=None,
        note="caps an investigation-framed-but-construction-discharged task's "
        "outcome to partial at record time.",
    ),
    ExperimentalFlag(
        flag="phantom_escalation_delivery_enabled",
        review_by="2027-07-31",
        gate="phantom_escalation_delivery",
        note="delivers a claimed escalation for a phantom-landed remediation.",
    ),
    ExperimentalFlag(
        flag="unconfirmed_landing_forced_enabled",
        review_by="2027-07-31",
        gate="unconfirmed_landing_forced",
        note="forces an unconfirmed-landing verdict to partial.",
    ),
    ExperimentalFlag(
        flag="pre_mint_generation_gate_enabled",
        review_by="2027-08-31",
        gate=None,
        note="proposal 120 pre-mint generation gate — the single pre-mint owner "
        "for both self-initiated plan-mint channels (evolve()'s fuel-gated "
        "propose_plan tail and _implement_approved_plans): generation_gate.may_mint_plan refuses a net-new mint on the same "
        "backlog/theme inputs the submission door reads; each refused channel "
        "skips its work and writes one prune/land directive, emits no "
        "gate_activations row.",
    ),
    ExperimentalFlag(
        flag="self_model_external_anchor_enabled",
        review_by="2027-08-31",
        gate=None,
        note="proposal 120 Phase C self-model external-capability anchor — "
        "update_self_model's limitations_known vanity filter gains a fifth, "
        "necessary admission test (a limitation qualifies only if it names an "
        "external-observable capability, behaviour, or audience effect, not "
        "solely the loop's own plan-mint / PR-remediation plumbing) and calls "
        "the inward-plumbing case out as vanity by example; a prompt-only edit "
        "behind the flag, emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="audit_flag_horizon_display_enabled",
        review_by="2027-08-31",
        gate=None,
        note="proposal 117 rollout-cadence sensor — injects a FLAG-HORIZON "
        "contemplation line naming EXPERIMENTAL_FLAGS entries within (or past) "
        "their review_by; pure display, self-suppressing, emits no gate row.",
    ),
    ExperimentalFlag(
        flag="audit_split_due_sensor_enabled",
        review_by="2027-10-31",
        gate=None,
        note="proposal 147 size-wall sensor — injects a SPLIT-DUE contemplation "
        "line naming source files at or above the split-due line; read-only, "
        "self-suppressing, emits no gate row.",
    ),
    ExperimentalFlag(
        flag="audit_split_due_directive_enabled",
        review_by="2027-10-31",
        gate="split_due_directive",
        note="proposal 147 split-due directive bridge — fills an empty "
        "next_task_directive with a concern-split directive for the worst "
        "split-due source file; self-extinguishing, emits a gate row per fill.",
    ),
    ExperimentalFlag(
        flag="multi_pr_proposals_enabled",
        review_by="2027-08-31",
        gate=None,
        note="proposal 118 chain-emission arm — evolve() decomposes a too-large "
        "intent into an ordered chain of slice plans owned by one "
        "EvolutionProposal and submits only the head slice for review; read in "
        "loop/self_health.evolve_with_fuel, emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="plan_grounding_enabled",
        review_by="2027-08-31",
        gate="plan_grounding",
        note="proposal 114 plan-premise grounding — propose_plan attaches a "
        "grounding probe and submit_for_review confirms it against a fresh "
        "reading of the tree as its first guard, recording a refuted-premise "
        "plan obsolete (gate=plan_grounding) before a human approval is spent.",
    ),
    ExperimentalFlag(
        flag="generator_reality_grounding_enabled",
        review_by="2027-09-30",
        gate="generator_reality_grounding",
        note="proposal 136 generator reality grounding — propose_plan declares "
        "new_capability_names (a gap plan's problem is its issue), and "
        "submit_for_review clears them against the reality inventory as its "
        "first guard, recording a plan that re-proposes a shipped capability "
        "obsolete "
        "(gate=generator_reality_grounding) before any human is notified; a "
        "cleared cli: declaration skips the shipped-CLI-surface guard, which "
        "stays the backstop for every other plan (the pre-mint reflect arm was "
        "deleted with reflect, proposal 155 step 4). The "
        "measured-problem ids reach the generator through 143's "
        "read_reality_inventory tool, so it is canaried alongside 143's.",
    ),
    ExperimentalFlag(
        flag="gate_observation_enabled",
        review_by="2027-09-30",
        gate="gate_convergence_terminal",
        note="proposal 124 gate observation — run_quality_gates parses each gate "
        "runner's retained output into a structured GateRunObservation "
        "(enumerated failures at failure-identity granularity, the TIMEOUT / "
        "UNAVAILABLE non-answers named instead of a 'could not run' string) and "
        "re-runs an -x-truncated pytest probe once without -x so the whole failing "
        "set is enumerated; also gates evolution._observe_baseline, which pays a "
        "whole extra gate sweep per new base commit (bounded to four cached "
        "baselines, off the event loop, and only on a tend lap that has an open "
        "evolution PR) so gate_observation.attribute can tell an inherited red "
        "from one the diff caused — so flipping this on has a real runtime cost, "
        "not just a parse; read in evolution.run_quality_gates and "
        "evolution._observe_baseline, and converge_to_green's terminal re-reads "
        "the gate at the not-fixed verdict and emits one gate_convergence_terminal "
        "row per convergence exit carrying the attribution verdict; the review "
        "loop's terminal (evolution._review_terminal) likewise re-reads the gate "
        "on the would-otherwise-be-stuck self-review round, buying a bounded "
        "verify-only round against a green branch instead of demoting on an "
        "undirtied tree, and emits one self_review_terminal row per review exit "
        "(the counted gate stays gate_convergence_terminal).",
    ),
    ExperimentalFlag(
        flag="gate_lease_cgroup_reserve_enabled",
        review_by="2026-12-31",
        gate=None,
        note="proposal 134 gate-fit budget — plan_lease omits the host-measured "
        "SYSTEM_RESERVE_MIB when the measured headroom was cgroup-bounded "
        "(HostCapacity.bounded_by == 'cgroup'), since memory.current has already "
        "charged the baseline the reserve holds back and double-counting it floors "
        "a containerised grant at one worker; off, the reserve applies to either "
        "bound and the grant is byte-identical to today. plan_lease is pure and "
        "emits no gate_activations row, so the effect is read from the "
        "available_mib / bounded_by fields stamped unconditionally on every "
        "gate_runs.jsonl row.",
    ),
    ExperimentalFlag(
        flag="gate_lease_admission_cap_enabled",
        review_by="2027-01-31",
        gate=None,
        note="gate admission cap — plan_lease shrinks the slot budget from cores "
        "to max(1, cores // MIN_WORKERS_PER_ADMITTED_RUN) and acquire_slot queues "
        "up to _CAPPED_ACQUIRE_WAIT_SECONDS before failing open, so concurrent "
        "suites stop timing each other out under the wall-clock pytest-timeout "
        "budget; off, slots == cores and the 30s wait are byte-identical to "
        "today. plan_lease is pure and emits no gate_activations row, so the "
        "effect is read from gate_runs.jsonl's in_flight / admission / "
        "wait_seconds fields and maestro measure-gate-contention.",
    ),
    ExperimentalFlag(
        flag="gate_budget_lease_owned_enabled",
        review_by="2026-12-31",
        gate=None,
        note="proposal 134 gate-fit budget — the gate execution lease owns the "
        "pytest run's deadline as well as its width: the grant carries a "
        "deadline_seconds derived by gate_capacity.derive_deadline from the "
        "gate_runs.jsonl rows at that same granted width, escalating 1.5x off the "
        "most recent in-process run killed at its own deadline, floored at "
        "[timeouts] selfcheck and capped at [timeouts] selfcheck_max, so a suite "
        "that genuinely needs more than the flat constant buys its way up to a fit "
        "instead of recording censored tests:TIMEOUT non-answers forever. Only the "
        "tests gate spends it; ruff and mypy keep selfcheck. Off, every gate runs "
        "under selfcheck and the argv and enforced timeout are byte-identical to "
        "today. The source / deadline_seconds telemetry fields are stamped "
        "regardless of the flag, so the flip is made against measured evidence — the "
        "derivation is pure and emits no gate_activations row, hence gate=None.",
    ),
    ExperimentalFlag(
        flag="gate_scope_impacted_enabled",
        review_by="2026-12-31",
        gate="gate_scope",
        note="proposal 134 gate-fit scope — the lease owns which suite a fix-loop "
        "round runs: converge_to_green's inner rounds call run_quality_gates with "
        "the impacted test modules gate_scope.impacted_tests selects from the "
        "plan's files_affected and --no-cov, comparing signatures restricted "
        "through gate_scope.restrict_signature so a narrowed suite can neither "
        "manufacture progress nor greenness; a green narrowed round is certified "
        "by one fresh full-scope, with-coverage read before green is a terminal, "
        "and the narrowed rows carry scope='impacted' so they never enter "
        "derive_deadline or measure-gate-contention. Off, the pre-loop read, every "
        "round and the argv are byte-identical to today. One gate_scope activation "
        "row per convergence records whether the narrow was taken.",
    ),
    ExperimentalFlag(
        flag="gate_environment_oracle_enabled",
        review_by="2026-12-31",
        gate="gate_environment_hold",
        note="proposal 133 pre-spend runnability consult — _implement_approved_plans "
        "refreshes gate_environment.refresh_verdict once per iteration before it "
        "builds its eligible list and, on a non-SOUND verdict, opens no "
        "implementation session: each plan it would have selected keeps its "
        "approved status and failure_count, stamped with the verdict's fingerprint "
        "so the hold is keyed to the fact rather than the clock, and Thomas is "
        "asked once per distinct (kind, identities) pair. The hold "
        "self-extinguishes — the next SOUND verdict releases every held plan. The "
        "same flag also arms the terminal arm: a convergence red whose every "
        "failing identity the cached verdict names is attributed ENVIRONMENT, and "
        "implement_plan holds the plan approved on it — stamping the fingerprint "
        "and charging no failure_count, no inherited_hold_count, no demote and no "
        "issue, so an environment that breaks mid-attempt costs one attempt's SDK "
        "spend and nothing else. The same flag binds the oracle into the gate "
        "execution lease: lease_for_gate_run reads (never refreshes) the cached "
        "verdict before it measures the host, and a verdict declines_gate_run "
        "accepts refuses the run outright — no host read, no slot, no ruff/mypy/"
        "pytest subprocess — yielding the verdict to run_quality_gates, which "
        "synthesizes the same failing COLLECTION_ERROR result a real run would "
        "have produced, and recording one admission='declined' telemetry row — excluded from the contention buckets and from the two lease rates' denominator — "
        "(measure-gate-contention's declined_rate is the evidence it fired). That consult sits below the lease's own rollback, so gate_lease_enabled=false disables the refusal too. Off, "
        "no probe is issued, no verdict is read, selection is byte-identical, "
        "every gate argv is unchanged and no ENVIRONMENT attribution can be "
        "produced. One gate_environment_hold activation row per held iteration, "
        "plus one per terminal hold.",
    ),
    ExperimentalFlag(
        flag="runtime_contract_enforced",
        review_by="2027-10-31",
        gate=None,
        note="proposal 141 step 8 — preflight and _build_agent (every "
        "agent-building command, serve included) fold "
        "deploy/runtime_contract.toml against the live environment offline "
        "(runtime_contract.enforce_runtime_contract); on, a required violation "
        "(or a missing/malformed contract) fails preflight and startup naming "
        "the entry, so safe-deploy rolls back; off, each violation is a WARNING "
        "and startup continues. Emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="host_capacity_sensor_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 125 host-capacity sensor — contemplate (and the loop's "
        "hoisted iteration-start snapshot it reuses) reads live host load per "
        "core, memory and swap residency, and free disk on the Docker root "
        "(host_capacity.read_host_capacity) onto the diagnostic snapshot, so "
        "summarize_health names any saturated resource and blind_spots stops "
        "reporting host capacity as unmeasured; off, no reading is taken and the "
        "health summary is byte-identical to today, while blind_spots still names "
        "host capacity as an unmeasured gap. Read in agent.contemplate and the "
        "loop's status hoist; emits no gate_activations row. The production "
        "canary is live (PROD_ONLY_CANARIES), calibrating the disk and swap "
        "thresholds over the deploy window.",
    ),
    ExperimentalFlag(
        flag="audit_self_calibration_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 129 self-calibration input capture — the priced reflection "
        "must state its loss condition in self_calibration.LOSS_CONDITION_GRAMMAR "
        "plus a numeric CONFIDENCE, and each prediction row gains the confidence "
        "and the parsed ProbeKind (or nothing when the grammar declines); no bet "
        "is scored, the PRIOR VERDICT self-grade is untouched. A prompt-shape "
        "change plus a ledger-field write, read by cli._build_agent and "
        "agent.contemplate, emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="audit_calibration_verdict_authority_enabled",
        review_by="2027-09-30",
        gate="calibration_verdict_authority",
        note="proposal 129 calibration verdict authority — agent.contemplate scores "
        "each open reflection bet through self_calibration.score_loss_condition and "
        "lets author_reflection_verdict, not the PRIOR VERDICT self-grade, author the "
        "reflection ledger verdict; the self-grade rides along as narration and each "
        "measured-vs-narrated divergence stamps a calibration_verdict_authority "
        "gate_activations row. Independent of audit_self_calibration_enabled (grammar "
        "capture); off, every persisted reflection_verdict row is byte-identical to "
        "today's.",
    ),
    ExperimentalFlag(
        flag="audit_closed_reference_enabled",
        review_by="2027-04-07",
        gate=None,
        note="rule 23 closed-reference claim detector — _audit_self_model resolves "
        "the #NNN numbers self-model entries cite through code_host.get_issue_state "
        "(capped lookups) and check_closed_reference_claim flags an entry whose every "
        "cited number is closed. Off: no lookup, run_all_checks byte-identical. Emits "
        "no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="audit_evidence_bound_claims_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 129 evidence-bound self-knowledge — update_self_model asks the "
        "model to cite each new/revised capabilities_known claim to something that "
        "already happened (a successful task id, an archived merge SHA, or an emitted "
        "behavioural-sensor tag) via a separate capability_evidence object, and the "
        "citation is parsed + resolved (self_calibration.parse_claim_citation / "
        "resolve_claim_citation) into SelfModel.claim_evidence recording existence + "
        "polarity, never entailment. Off: no response key is read, no map entry is "
        "written, and the update prompt is byte-identical (the empty map is omitted "
        "from the serialised model). Written in agent.contemplate (which builds the "
        "ClaimEvidenceSources bundle only when on); read back in loop.self_audit.audit_self_model "
        "via run_all_checks, where check_unbound_capability_claim surfaces every "
        "capabilities_known claim whose recorded evidence is absent or non-resolved as "
        "an [audit] open question. Emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="audit_refutation_accountability_enabled",
        review_by="2027-09-30",
        gate="refutation_accountability",
        note="proposal 129 Phase C refutation accountability — after persisting the "
        "audit cycle, loop.self_audit.audit_self_model runs "
        "audit_checks.measure_refutation_rejections: a refuted [audit refuted] open "
        "question whose rule is classed mechanical_fact in RULE_AUTHORITY (a "
        "git-ancestry read, a plan-store transition, or the message/plan ledger) and "
        "that keeps re-firing on the same target for "
        "audit_refutation_rejection_cycles consecutive cycles is mechanically "
        "restored to its canonical [audit] form, the override stamped to the journal "
        "and a refutation_accountability gate_activations row, and surfaced on the "
        "REALITY-REFUTATION contemplation line. Derived measures and heuristics stay "
        "refutable indefinitely; self-extinguishing. Off: open_questions and the gate "
        "trace are byte-identical.",
    ),
    ExperimentalFlag(
        flag="conscious_workspace_broadcast_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 130 conscious-access workspace broadcast — the selected "
        "ConsciousMoment is rendered to a bounded block "
        "(conscious_workspace.render_active_moment) and prepended, per call, to "
        "three cognitive consumers: contemplation and evolution (which route through "
        "consciousness.introspect's workspace_moment param) and task generation "
        "(prepended in TaskGenerator.generate_task, not via introspect); each "
        "delivery records a conscious_workspace.BroadcastReceipt keyed to the moment's "
        "selection_hash; the block is advisory prompt text — no capability, no plan "
        "approval, no gate consults it. Off, every consumer receives an empty block "
        "and every prompt is byte-identical; a no-op unless conscious_workspace_"
        "observe_enabled is also on (no attend pass means no moment). Read in "
        "agent._deliver_moment_to_consumer (behind the _broadcast_moment_prefix "
        "block-only wrapper and the evolve + task-generation citation paths, the "
        "latter two calling it directly to stamp cited item ids), emits no "
        "gate_activations row.",
    ),
    ExperimentalFlag(
        flag="attempt_disposition_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 131 attempt-disposition oracle — a post-implementation "
        "quality-gate convergence that stalls is routed through "
        "attempt_disposition.classify_attempt at agent.implement_plan's "
        "non-inherited stuck terminal. RESUME (a strictly-progressing stall whose "
        "failing-identity set is a strict subset of the prior attempt's, within "
        "max_resume_attempts): preserves the evolution branch, persists the failing "
        "baseline plus resume_branch_head and an incremented resume_count (with "
        "failure_count frozen), and leaves the plan re-admittable, while "
        "evolution.implement checks out that preserved branch, merges origin/main, "
        "and prepends a bounded resume preamble naming the prior failing "
        "identities. ASK_HUMAN (a recurring environment/infrastructure red no code "
        "change can clear — a collection abort, a runner timeout, an unrunnable "
        "gate): asks Thomas one specific question via ask_thomas and parks the plan "
        "in the inert 'blocked' status keyed to that question's message id — no "
        "demote, no spent attempt, no GitHub issue. The reply then closes the loop: "
        "agent._process_messages maps blocked plans by blocked_question_id and, when "
        "an inbound reply from Thomas threads (via reply_to) back to one, the reply "
        "is dispatched by its content: an explicit decision to abandon the plan "
        "(_is_abandonment_reply, a conservative negation-guarded read that defaults "
        "to re-admission) routes through agent._abandon_blocked_plan to terminal "
        "'obsolete' (mark_plan_obsolete's allow_blocked opt-in — no priced "
        "implement call), while any other answer routes through "
        "agent._readmit_blocked_plan, which sets the plan 'approved', clears "
        "blocked_question_id, and stamps the answer as blocked_answer. "
        "evolution.implement prepends that answer as a bounded blocked-answer "
        "preamble to the next implementation prompt and clears it one-shot so a "
        "later retry does not re-inject it. Every other verdict degrades to the "
        "ordinary demote. Off: no classify_attempt call, no blocked-plan map (the "
        "message drain is byte-identical), and the branch lifecycle plus "
        "implementation prompt are byte-identical to today. Read in "
        "agent.implement_plan, agent._process_messages, agent._readmit_blocked_plan, "
        "agent._abandon_blocked_plan, and evolution.implement, emits no "
        "gate_activations row.",
    ),
    ExperimentalFlag(
        flag="stale_repeat_bet_guard_enabled",
        review_by="2027-09-30",
        gate=None,
        note="stale-repeat bet guard — agent.contemplate computes the set of "
        "loss-condition strings already staked at/above the repeat threshold "
        "without ever scoring 'correct' (the comfort bet) via "
        "self_calibration.stale_repeat_loss_conditions and passes it into the "
        "priced-reflection prompt, which names each under a 'do not reuse' block "
        "so the next bet cannot verbatim-repeat a stale loser (whitespace/case-"
        "normalised matching only). Off: the banned set is empty and the rendered "
        "prompt is byte-identical. Read in agent.contemplate; a prompt-only edge, "
        "emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="grounded_reflection_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 132 grounded contemplation — gates building the priced "
        "contemplation (slot-A) reflection session with the bounded, structurally "
        "read-only reflection_retrieval surface "
        "(build_bounded_reflection_tool_set) instead of tools=[], so a falsifiable "
        "bet can pull the one fact it hinges on. Read by "
        "MaestroAgent._grounded_reflection_active (which also degrades under "
        "budget_throttle_active) and _build_contemplation_session_options; "
        "flag-off keeps the session tools=[] and byte-identical. Emits no "
        "gate_activations row.",
    ),
    ExperimentalFlag(
        flag="grounded_planning_enabled",
        review_by="2027-10-01",
        gate=None,
        note="proposal 143 grounded planning — gates building the single-use "
        "sessions of the planner callers listed in grounded_planning_callers "
        "(propose_plan, and opt-in propose_plan_chain / curate_pending_plans) with the bounded "
        "read-only tree surface "
        "(tree_retrieval.build_bounded_tree_tool_set) instead of tools=[], so "
        "those evolve-phase calls read the paths and symbols they cite and each "
        "plan (or chain slice) stamps grounded_reads; the surface's read_reality_inventory tool renders the "
        "reality inventory so the planner reads shipped capabilities and measured "
        "problem ids rather than guessing them. Read by the evolve phase in MaestroAgent (which also "
        "degrades under budget_throttle_active), "
        "EvolutionEngine._build_tree_read_options and "
        "required_capabilities_for_phase; flag-off keeps the call tools=[] and "
        "byte-identical. Emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="grounded_planning_rejection_tool_enabled",
        review_by="2027-10-01",
        gate=None,
        note="grounded planner rejection-ledger search — when on (and grounded "
        "planning active), EvolutionEngine._build_tree_read_options supplies the "
        "bounded rejected-plan corpus to tree_retrieval.build_bounded_tree_tool_set "
        "so the planner carries a sixth pure tool, search_rejections, querying the "
        "specific ideas already refused and their reasons at mint time rather than "
        "only the aggregate category counts read_reality_inventory surfaces. Off: "
        "the five-tool set is unchanged. Emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="cooldown_signature_keying_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 137 cooldown signature keying — both tend cooldowns "
        "(_in_conflict_cooldown, _in_quality_fix_cooldown) compare the current "
        "signature against the one stamped on the anchor, and a provably-changed "
        "component releases the cooldown before the hours window is consulted, so "
        "a verdict about a replaced head cannot bounce the walk for the rest of a "
        "24h window. The conflict cooldown keys on the full (branch head, "
        "origin/main base, CI rollup conclusion) triple; the quality-fix one keys "
        "on (branch head, CI rollup conclusion), since an origin/main advance says "
        "nothing about whether this PR's own red changed. Every component is a "
        "local the tend walk already measured, so no extra host or VCS call. Off, "
        "both predicates are the pure wall-clock windows "
        "(including the conflict cooldown's own base-SHA release) and the walk is "
        "byte-identical; the anchor stamping itself is unconditional bookkeeping. "
        "The same flag also gates the second half of the instrument: on an "
        "UNCHANGED signature the flat hours window is replaced by a bounded "
        "exponential backoff over the laps already spent against it — "
        "[loop] cooldown_backoff_base_hours for the first re-route, doubling "
        "each unchanged lap, capped at the cooldown's own hours ceiling — so a "
        "lap that changed nothing is retried within the hour while a "
        "persistently-broken head converges back to the 24h cadence. The "
        "per-signature attempt counts are stamped unconditionally too, but a "
        "rung is *consumed* only by a lap the cooldown genuinely governed — a "
        "bypassed (escape-hatch-admitted) lap, a lap that itself pushed the "
        "branch, and a lap that could not measure its own head or rollup all "
        "leave the count where it was. "
        "Read in evolution_tend._in_conflict_cooldown / _in_quality_fix_cooldown "
        "via _cooldown_window_hours; emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="owed_act_binding_enabled",
        review_by="2027-09-30",
        gate=None,
        note="proposal 137 owed-act binding — the two give-up decisions that still ran on weaker facts read the pr_owed_act oracle's verdict, derived from a readiness fact read through the per-iteration-memoized observe_pr_readiness chokepoint, so the binding costs at most one readiness observation per cited PR and reuses the memo whenever an earlier phase this iteration already observed that PR. Audit rule 21 (check_reconcile_near_miss) suppresses the 'or gh pr close #N' clause of its finding while the PR reads CODE_OWED — a failing gate the loop can fix itself is not a co-equal alternative to closing — and assess_pr_salvage returns SALVAGE_AND_LAND ahead of its disposable arm for code-owed work still inside RECONCILE_COMMITS_BEHIND_BUDGET, so the veto over give-up holds while the fix is cheap. Past the budget nothing changes: an uncurable PR can still be closed. Read by MaestroAgent._audit_self_model's rule-21 block and _assess_pr_salvage via the loop.owed_act_binding collaborator; off, the finding text and the whole salvage verdict table are byte-identical. Emits no gate_activations row — the binding changes what an existing predicate returns.",
    ),
    ExperimentalFlag(
        flag="planning_excludes_remediation_enabled",
        review_by="2027-01-31",
        gate=None,
        note="drops Remediate PR #<n>: laps from evolution planning's recent-"
        "task window via remediation_tracker.recent_planning_tasks, read by "
        "EvolutionEngine._gap_context_inputs and _validate_source_task_id, so a lap's open "
        "PR head is never advertised or accepted as a source_task_id base; implement() re-checks via planning_base_branch.",
    ),
    ExperimentalFlag(
        flag="loop_plan_feedback_enabled",
        review_by="2027-10-31",
        gate=None,
        note="proposal 145 reviewer feedback on pending plans — "
        "select_curation_target curates a pending_review plan carrying "
        "unaddressed reviewer_feedback first, bypassing the curate grace "
        "floor. Read by EvolutionEngine.curate_pending_plans and "
        "MaestroAgent._curate_pending_plans; off, the selector is "
        "byte-identical. The curator then answers the feedback: "
        "render_feedback_section adds it to the curate prompt and "
        "curate_with_feedback requires a response, downgrades obsolete "
        "to keep, and stamps the rendered entries addressed; a three-"
        "strike release (MAX_FEEDBACK_CURATE_ATTEMPTS) frees feedback the "
        "curator cannot act on so one plan cannot starve the rest. The "
        "flag also arms an off-cadence curate fire while a pending plan "
        "holds unaddressed feedback, read by "
        "LifecycleController.phase_will_run. Emits no gate_activations row.",
    ),
    ExperimentalFlag(
        flag="loop_curate_skip_serviced_enabled",
        review_by="2027-10-31",
        gate=None,
        note="caps curate re-spend — select_curation_target excludes from the "
        "normal (non-feedback) curation lanes every plan already serviced once "
        "(refined_at OR last_curated_at not None), so curate_pending_plans "
        "takes its zero-cost None return even after a serviced plan's content "
        "or feedback changes (the curated_hash skip already drops unchanged "
        "plans). Read by "
        "EvolutionEngine.curate_pending_plans and "
        "MaestroAgent._curate_pending_plans; off, the selector is byte-"
        "identical. The reviewer-feedback priority lane is exempt. Emits no "
        "gate_activations row.",
    ),
    ExperimentalFlag(
        flag="loop_obsolete_directive_actuator_enabled",
        review_by="2027-04-30",
        gate="obsolete_directive_actuator",
        note="audit_self_model fires mark_plan_obsolete (fire_and_capture + "
        "verify_obsolete_persisted) on the single pending_review plan a "
        "next_task_directive names for that lever, records an owned_prune deed "
        "and clears the directive; a single named plan already off "
        "pending_review just clears it. Runs with self_directed_tasks_enabled "
        "off. Off, the directive and plan store are untouched.",
    ),
)


#: The default-on cut-over flags live on today's tree. A source census finds ten:
#: seven genuine collapse targets once burned in (the proposal-089
#: observed ready-and-waiting stand-down ``observed_ready_standdown_enabled``,
#: graduated 2026-08-25, the committed-but-unpushed push actuator
#: ``owned_committed_push_actuator_enabled``, graduated 2026-08-26, the
#: proposal-122 account-dashboard navigation shell ``account_ui_v2``, graduated
#: 2026-09-03, and the proposal-128 merge-queue cutover kill-switch
#: ``tend_main_merge_enabled``, plus the proposal-130 observe-only attend phase
#: ``conscious_workspace_observe_enabled``, flipped on 2026-09-30, and the
#: proposal-116 plan-mill closure levers ``generation_admission_enabled`` and
#: ``retire_unlanded_limitations_enabled``, graduated 2026-10-07) and three whose off-path doubles as a permanent
#: fallback (``enable_session_reuse``, ``structured_transcripts_enabled``,
#: ``gate_lease_enabled``), recorded ``retain`` with their reason. The first
#: genuine collapse target the census found,
#: ``live_work_corpus_enabled``, was collapsed and its entry dropped (proposal 111) —
#: the runbook this mechanism exists to serve. A
#: bounded, self-pruning registry: :func:`find_stage_violations` reds the build the
#: moment an entry's stage and its off-path fall out of step, so a completed collapse
#: must drop its entry rather than leaving a marker behind.
CUTOVER_FLAGS: tuple[CutoverFlag, ...] = (
    CutoverFlag(
        flag="enable_session_reuse",
        proposal=None,
        cutover_cycle="2026-04-18",
        off_path_symbol="_get_or_create_session",
        review_by="2027-04-30",
        stage="retain",
        note="stateless per-call query() off-path doubles as the create_session-"
        "failure fallback and is hard-coupled to sdk_phase_providers in "
        "Config.validate; retained, not a collapse target.",
    ),
    CutoverFlag(
        flag="structured_transcripts_enabled",
        proposal=104,
        cutover_cycle="2026-08-01",
        off_path_symbol="configure_sdk_transcripts",
        review_by="2027-07-31",
        stage="retain",
        note="raw-.log rendering fallback for pre-rollout sessions with no "
        "adjacent sidecar; retained, not a collapse target.",
    ),
    CutoverFlag(
        flag="observed_ready_standdown_enabled",
        proposal=89,
        cutover_cycle="2026-08-25",
        off_path_symbol="observed_ready_standdown_enabled",
        review_by="2027-02-28",
        stage="cutover",
        note="proposal 089's observed ready-and-waiting dequeue gate "
        "(_gate_pr_remediation_ready), graduated 2026-08-25 from default-off to "
        "base default-on: it zero-cost-evicts a ready_and_waiting owned PR from "
        "the remediation dequeue with a Marker.READY partial rather than "
        "re-servicing it. Flag-off makes the gate return False immediately, "
        "leaving the dequeue chain byte-identical to the pre-089 chain. Collapse "
        "deletes the field and its guard branch, so it is its own "
        "off_path_symbol; collapse only after the ready-and-waiting re-service "
        "treadmill is confirmed ended without suppressing self-clearable work.",
    ),
    CutoverFlag(
        flag="owned_committed_push_actuator_enabled",
        proposal=None,
        cutover_cycle="2026-08-26",
        off_path_symbol="owned_committed_push_actuator_enabled",
        review_by="2027-02-28",
        stage="cutover",
        note="the committed-but-unpushed push actuator "
        "(_push_committed_unpushed_owned_head), graduated 2026-08-26 from "
        "default-off to base default-on: in _record_remediation_outcome it "
        "fires the owed git push on a gate-clean committed owned head ahead of "
        "origin/<pr_branch> under an AWAITING_PUSH blocker (green-gated, "
        "RED reset-to-origin rewind, delivery bound to "
        "verify_push_advanced_remote_head). Flag-off leaves the arm inert so the "
        "push surfaces only as an OWNED_PUSH_AVAILABLE marker, byte-identical to "
        "the pre-promotion path. Collapse deletes the field and makes the "
        "actuator unconditional, so it is its own off_path_symbol; collapse only "
        "after the AWAITING_PUSH treadmill is confirmed ended.",
    ),
    CutoverFlag(
        flag="account_ui_v2",
        proposal=122,
        cutover_cycle="2026-09-03",
        off_path_symbol="account_ui_v2",
        review_by="2027-03-31",
        stage="cutover",
        note="proposal 122's account-dashboard navigation shell, graduated "
        "2026-09-03 from a default-off dark launch to base default-on: the "
        "/account handler stamps a data-account-ui-v2 marker onto <body> and "
        "enhanceNavV2() turns the flat pill row into the grouped ARIA tablist "
        "with a sidebar rail at >=1024px. Flag-off serves the template "
        "byte-identical, so the flat nav is a one-line rollback for the "
        "observation window. The flag-off branch lives in HTML/CSS/JS that the "
        "Python-only symbol scan cannot see, so the field is its own "
        "off_path_symbol — deleting it is what marks the collapse; collapse only "
        "after the window closes with no /account accessibility regression.",
    ),
    CutoverFlag(
        flag="gate_lease_enabled",
        proposal=126,
        cutover_cycle="2026-09-08",
        off_path_symbol="worker_args",
        review_by="2027-09-30",
        stage="retain",
        note="proposal 126's gate execution lease, graduated 2026-09-08 from a "
        "default-off experiment to base default-on to fix #3760: the maestro "
        "gate-lease wrapper and the in-process gate readers "
        "(evolution.run_quality_gates, gate_check.run_gate_check, "
        "pr_remediation.preflight) route through gate_capacity.lease_for_gate_run, "
        "measuring the container-tight host capacity (the cgroup limit, not the "
        "host it shares) and running pytest at the bounded granted width instead "
        "of a host-sized -n auto that OOM-thrashed the 2 GiB loop cgroup. Flag-off "
        "makes Config.gate_lease_dir None, so lease_for_gate_run yields None and "
        "worker_args(None) renders the empty argv — the child runs at today's auto "
        "width, byte-identical to the pre-lease path. The auto-width fallback is a "
        "deliberately retained rollback (pinned as a permanent carve-out in "
        "tests/test_gate_lease_owner.py), so worker_args is the permanent "
        "off_path_symbol that keeps it wired; stage=retain, not a collapse target.",
    ),
    CutoverFlag(
        flag="tend_main_merge_enabled",
        proposal=128,
        cutover_cycle="2026-09-09",
        off_path_symbol="tend_main_merge_enabled",
        review_by="2027-09-30",
        stage="cutover",
        note="proposal 128's merge-queue cutover kill-switch, default-on: gates "
        "_tend_single_pr's origin/main merge-and-push. Flag-off makes the tend "
        "phase skip the eager main-resync (a merge queue tests each PR once at the "
        "front against the state it lands into) while keeping every other tend "
        "role — quiet-PR reminders, conflict routing via the merge_tree_conflicts "
        "probe, quality-gate routing, and delivery of a local remediation commit "
        "already on the branch. Flag-on is byte-identical to the pre-128 tend "
        "phase. The tend merge emits no gate_activations.jsonl row (gate=None). "
        "Collapse deletes the field and its guard branch, so it is its own "
        "off_path_symbol (the observed_ready_standdown_enabled / account_ui_v2 "
        "idiom); collapse only after the merge queue is proven live and the eager "
        "resync is confirmed retired.",
    ),
    CutoverFlag(
        flag="conscious_workspace_observe_enabled",
        proposal=130,
        cutover_cycle="2026-09-30",
        off_path_symbol="conscious_workspace_observe_enabled",
        review_by="2027-03-31",
        stage="cutover",
        note="proposal 130 step 11 blind burn-in: observation flipped on "
        "everywhere in one reversible base change (the proposal rules out a "
        "prod-only canary — a fleet of one gains no isolation from environment "
        "divergence). On, the zero-SDK attend phase between tend_evolution_prs "
        "and contemplate assembles one ConsciousMoment and appends one episode "
        "per pass; the moment is injected nowhere (broadcast stays dark behind "
        "conscious_workspace_broadcast_enabled until step 12's human go/no-go). "
        "Flag-off, read in loop.lifecycle.phase_will_run, the phase never runs "
        "and state writes are byte-identical — the rollback. Window: >=100 "
        "completed episodes and >=14 days, whichever is later; criteria in the "
        "'Conscious-access observation burn-in (proposal 130)' subsection of "
        "docs/self-evolution.md. Step 13 collapses the field and its guard "
        "branch on positive evidence, or deletes the workspace, so the flag is "
        "its own off_path_symbol. Emits no gate_activations row. Proposal 149 "
        "flipped base off: no moment was ever cited, so this zero-citation "
        "writer is stopped; collapse is left to the workspace decommission.",
    ),
    CutoverFlag(
        flag="generation_admission_enabled",
        proposal=116,
        cutover_cycle="2026-10-07",
        off_path_symbol="generation_admission_enabled",
        review_by="2027-04-30",
        stage="cutover",
        note="proposal 116's evolution-plan admission gate, graduated 2026-10-07 "
        "from a prod-only canary to base default-on after the 'Plan-mill closure "
        "canary (proposal 116)' window moved every criterion the right way. "
        "Flag-off skips the guard branch in Evolution.submit_for_review, "
        "restoring the un-gated submission. Emits no gate_activations row. "
        "Collapse deletes the field and makes the guard unconditional once a "
        "further window confirms no starved legitimate mint, so the field is its "
        "own off_path_symbol.",
    ),
    CutoverFlag(
        flag="retire_unlanded_limitations_enabled",
        proposal=116,
        cutover_cycle="2026-10-07",
        off_path_symbol="retire_unlanded_limitations_enabled",
        review_by="2027-04-30",
        stage="cutover",
        note="proposal 116's retire-unlanded-limitations fuel-starve, graduated "
        "2026-10-07 from a prod-only canary to base default-on after the "
        "'Plan-mill closure canary (proposal 116)' window moved every criterion "
        "the right way. Flag-off skips the guard branch in contemplate, "
        "restoring mirroring of every limitation. Emits no gate_activations "
        "row. Collapse deletes the field and makes the guard unconditional once "
        "a further window confirms no over-filtered genuine limitation, so the "
        "field is its own off_path_symbol.",
    ),
)


#: Time-boxed prod-only canaries: flags enabled in ``maestro.production.toml`` only
#: to gather evidence under a documented graduation criterion, each *meant* to
#: converge (promote to base or revert). Their ``review_by`` horizons are the
#: forcing function ``test_no_prod_only_canary_is_overdue`` enforces. Every horizon
#: is future-dated relative to the day this manifest was authored (2026-08-21),
#: mirroring the ``EXPERIMENTAL_FLAGS`` idiom — a crossed horizon is the intended
#: red-build signal, not a same-week emergency — so a canary whose documented window
#: already elapsed (the proposal-090 set) carries a near-future *grace* horizon with
#: the true deploy date + window recorded in its ``note``. Note that
#: ``phantom_escalation_delivery_enabled``, ``unconfirmed_landing_forced_enabled``,
#: ``pre_mint_generation_gate_enabled``, ``self_model_external_anchor_enabled``,
#: ``refutable_standdown_enabled``, ``sdk_single_use_evolution_calls_enabled``,
#: ``self_review_own_session_enabled``, ``sdk_call_policy_resolution_enabled``,
#: ``gate_environment_oracle_enabled``, ``gate_budget_lease_owned_enabled``,
#: ``host_capacity_sensor_enabled``, ``grounded_planning_enabled``,
#: ``grounded_planning_rejection_tool_enabled``,
#: ``loop_plan_feedback_enabled``,
#: and ``generator_reality_grounding_enabled`` also
#: appear in
#: :data:`EXPERIMENTAL_FLAGS` on the *base default-off* axis — the
#: base opt-in horizon and this overlay-enable horizon are distinct concerns and are
#: deliberately not conflated into one entry.
PROD_ONLY_CANARIES: tuple[EnvOverlayCanary, ...] = (
    EnvOverlayCanary(
        flag="context7_mcp_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 084 Context7 canary (TOML [mcp] context7_enabled); "
        "graduation criteria in docs/operations/context7-canary-delta.md — a "
        "measurement methodology, not a fixed calendar window; deployed 2026-06-21 "
        "(5e4fd220).",
    ),
    EnvOverlayCanary(
        flag="phantom_escalation_delivery_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 090 forced-arm canary; criteria in the 'Forced-arm "
        "graduation criteria (proposal 090)' subsection of docs/self-evolution.md; "
        "deployed 2026-07-11 (a9320f3c), 30-day window (elapsed 2026-08-10) — "
        "near-future grace horizon keeps the lateness a live signal. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern). Re-dated "
        "2026-10-01 from 2026-09-30: deferral only, no graduation verdict yet.",
    ),
    EnvOverlayCanary(
        flag="unconfirmed_landing_forced_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 090 forced-arm canary; criteria in the 'Forced-arm "
        "graduation criteria (proposal 090)' subsection of docs/self-evolution.md; "
        "deployed 2026-07-11 (a9320f3c), 30-day window (elapsed 2026-08-10) — "
        "near-future grace horizon keeps the lateness a live signal. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern). Re-dated "
        "2026-10-01 from 2026-09-30: deferral only, no graduation verdict yet.",
    ),
    EnvOverlayCanary(
        flag="audit_reality_refutation_display_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 090 reality-refutation watchdog canary (TOML [audit] "
        "reality_refutation_display_enabled); criteria in the 'Forced-arm "
        "graduation criteria (proposal 090)' subsection of docs/self-evolution.md; "
        "deployed 2026-07-11 (a9320f3c), 30-day window (elapsed 2026-08-10) — "
        "near-future grace horizon keeps the lateness a live signal. Re-dated "
        "2026-10-01 from 2026-09-30: deferral only, no graduation verdict yet.",
    ),
    EnvOverlayCanary(
        flag="audit_reality_refutation_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 090 reality-refutation watchdog canary (TOML [audit] "
        "reality_refutation_enabled); criteria in the 'Forced-arm graduation "
        "criteria (proposal 090)' subsection of docs/self-evolution.md; deployed "
        "2026-07-11 (a9320f3c), 30-day window (elapsed 2026-08-10) — near-future "
        "grace horizon keeps the lateness a live signal. Re-dated "
        "2026-10-01 from 2026-09-30: deferral only, no graduation verdict yet.",
    ),
    EnvOverlayCanary(
        flag="gate_observation_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 124 attributed gate observation canary; criteria in the "
        "'Attributed gate observation canary (proposal 124)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "14-day observation window. Also in EXPERIMENTAL_FLAGS on the base "
        "default-off axis (distinct concern).",
        justification="the stale-red and INHERITED-demotion evidence only accrues "
        "from the live loop's real convergence and landing terminals (the "
        "gate_convergence_terminal activation rows); flag-off is byte-identical, so "
        "a dark-launched off-everywhere build would gather none — the observation "
        "must run where the PRs are.",
    ),
    EnvOverlayCanary(
        flag="pre_mint_generation_gate_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 120 pre-mint generation authority canary; criteria in the "
        "'Pre-mint generation authority canary (proposal 120)' subsection of "
        "docs/self-evolution.md; deployed 2026-09-07 on the production overlay "
        "merge with a one-production-dream-window observation period. Shipped in "
        "one canary with self_model_external_anchor_enabled (Step 9 depends on "
        "Step 7). Also in EXPERIMENTAL_FLAGS on the base default-off axis "
        "(distinct concern).",
        justification="Observability, not a feature-gate: the per-channel "
        "dead-on-arrival-mints evidence Step 9's decommission is gated on accrues "
        "only where the autonomous loop actually mints plans (production), and the "
        "saturation-only pre-mint arm (theme judged per-candidate at the "
        "submission door since #4151) must be watched under real backlog "
        "conditions before this can graduate to a base default.",
    ),
    EnvOverlayCanary(
        flag="self_model_external_anchor_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 120 pre-mint generation authority canary (Phase C anchor); "
        "criteria in the 'Pre-mint generation authority canary (proposal 120)' "
        "subsection of docs/self-evolution.md; deployed 2026-09-07 on the "
        "production overlay merge with a one-production-dream-window observation "
        "period. Shipped in one canary with pre_mint_generation_gate_enabled (Step "
        "9 depends on Step 7). Also in EXPERIMENTAL_FLAGS on the base default-off "
        "axis (distinct concern).",
        justification="Observability, not a feature-gate: the Phase C anchor must "
        "be burned in before the Step 9 decommission lands, and its effect on the "
        "limitations_known admission rate can only be measured where the loop runs "
        "the priced update_self_model against a real backlog (production), so it is "
        "a prod-only canary rather than a dark-launched off-everywhere feature.",
    ),
    EnvOverlayCanary(
        flag="grounded_reflection_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 132 grounded contemplation canary; criteria in the "
        "'Grounded contemplation canary (proposal 132)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "one-production-dream-window observation period. Also in EXPERIMENTAL_FLAGS "
        "on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the RCA #3842 introspect "
        "terminal-error crash class only recurs (or stays closed) under the live "
        "priced reflection, and reflection-cited premise accuracy can only be "
        "measured where the loop runs contemplation against a real backlog "
        "(production); flag-off is byte-identical, so a dark-launched off-everywhere "
        "build would gather no crash-recurrence or premise-accuracy evidence — the "
        "observation must run where the loop reflects.",
    ),
    EnvOverlayCanary(
        flag="attempt_disposition_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 131 attempt-disposition oracle canary; criteria in the "
        "'Attempt-disposition oracle canary (proposal 131)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "one-production-dream-window observation period. Also in EXPERIMENTAL_FLAGS "
        "on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the RESUME / ASK_HUMAN / "
        "HOLD_INHERITED / ABANDON disposition split only moves terminal-failed "
        "counts, attempts-per-failed-plan, and the new blocked/resume cohorts where "
        "the loop runs the implement lineage against a real backlog (production); "
        "flag-off is byte-identical, so a dark-launched off-everywhere build would "
        "gather no disposition evidence — the observation must run where plans are "
        "implemented.",
    ),
    EnvOverlayCanary(
        flag="plan_grounding_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 114 plan-premise grounding canary; criteria in the "
        "'Plan-premise grounding canary (proposal 114)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "one-production-dream-window observation period. Also in EXPERIMENTAL_FLAGS "
        "on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the ungrounded-premise "
        "rejection rate the grounding guard produces only accrues where the "
        "autonomous loop actually authors and submits plans against a real backlog "
        "(production) — the UNGROUNDED-RATE contemplation sensor and the "
        "[ungrounded:<category>] obsolete reasons; flag-off is byte-identical, so a "
        "dark-launched off-everywhere build would gather no false-rejection "
        "evidence — the observation must run where plans are submitted.",
    ),
    EnvOverlayCanary(
        flag="audit_self_calibration_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 129 consciousness-as-measured-function canary (TOML [audit] "
        "self_calibration_enabled); criteria in the 'Consciousness measured-function "
        "canary (proposal 129)' subsection of docs/self-evolution.md; deployed by "
        "this PR's production merge with a one-production-dream-window observation "
        "period. Also in EXPERIMENTAL_FLAGS on the base default-off axis (distinct "
        "concern).",
        justification="Observability, not a feature-gate: a scored self-prediction "
        "track record only accrues where the priced reflection actually confronts "
        "resolved bets against measured task/gate/plan history (production) — the "
        "SELF-CALIBRATION contemplation line and the predictions ledger; flag-off is "
        "byte-identical, so a dark-launched off-everywhere build would gather no "
        "calibration evidence — the observation must run where the loop reflects.",
    ),
    EnvOverlayCanary(
        flag="audit_calibration_verdict_authority_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 129 consciousness-as-measured-function canary (TOML [audit] "
        "calibration_verdict_authority_enabled); criteria in the 'Consciousness "
        "measured-function canary (proposal 129)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "one-production-dream-window observation period. Also in EXPERIMENTAL_FLAGS "
        "on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: measured-vs-narrated "
        "verdict agreement/divergence only accrues where the oracle re-scores open "
        "bets each cycle against real settlement (production) — the "
        "calibration_verdict_authority gate-activation rows and the by_verdict_source "
        "split; flag-off is byte-identical, so a dark-launched off-everywhere build "
        "would gather no divergence evidence — the observation must run where the "
        "loop reflects.",
    ),
    EnvOverlayCanary(
        flag="audit_evidence_bound_claims_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 129 consciousness-as-measured-function canary (TOML [audit] "
        "evidence_bound_claims_enabled); criteria in the 'Consciousness "
        "measured-function canary (proposal 129)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "one-production-dream-window observation period. Also in EXPERIMENTAL_FLAGS "
        "on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: whether capability claims "
        "carry resolvable evidence only accrues where update_self_model authors real "
        "claims against live task/SHA/sensor anchors (production) — the "
        "SelfModel.claim_evidence resolution statuses and the unbound-claim [audit] "
        "questions; flag-off writes nothing, so a dark-launched off-everywhere build "
        "would gather no citation-resolution evidence — the observation must run "
        "where the self-model is authored.",
    ),
    EnvOverlayCanary(
        flag="audit_refutation_accountability_enabled",
        env="production",
        review_by="2026-11-30",
        note="proposal 129 consciousness-as-measured-function canary (TOML [audit] "
        "refutation_accountability_enabled); criteria in the 'Consciousness "
        "measured-function canary (proposal 129)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "one-production-dream-window observation period. Also in EXPERIMENTAL_FLAGS "
        "on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: whether a refuted "
        "mechanical_fact finding keeps recurring across the threshold only accrues "
        "where the live loop re-audits real git/ledger state each cycle (production) "
        "— the refutation_accountability gate-activation rows and the "
        "REALITY-REFUTATION contemplation clause; flag-off keeps open_questions "
        "byte-identical, so a dark-launched off-everywhere build would gather no "
        "recurrence evidence — the observation must run where the audit re-fires.",
    ),
    EnvOverlayCanary(
        flag="undercredited_landing_enabled",
        env="production",
        review_by="2026-12-31",
        note="issue #3280 Tier 1 landing-credit grounding canary (TOML [audit] "
        "undercredited_landing_enabled, audit rule 20); criteria in the "
        "'Landing-credit grounding canary (issue #3280)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge.",
        justification="Observability, not a feature-gate: whether a landing git "
        "proves happened is being under-credited only accrues where the loop "
        "authors real commits against a real origin/main (production) — the rule's "
        "[audit] findings and audit_history.jsonl rows are computed from "
        "extract_self_committed_shas over live task history, so a dark-launched "
        "off-everywhere build would gather no under-credit evidence.",
    ),
    EnvOverlayCanary(
        flag="reconcile_near_miss_enabled",
        env="production",
        review_by="2026-12-31",
        note="issue #3280 Tier 1 landing-credit grounding canary (TOML [audit] "
        "reconcile_near_miss_enabled, audit rule 21); criteria in the "
        "'Landing-credit grounding canary (issue #3280)' subsection of "
        "docs/self-evolution.md; reconcile_near_max_behind inherits its base "
        "value; deployed by this PR's production merge.",
        justification="Observability, not a feature-gate: the ≤N-behind reconcile "
        "band only exists where real own PRs drift behind a real origin/main "
        "(production) — the rule measures commits_behind and merge truth on live "
        "host state, so off-everywhere there is no PR to measure and no "
        "'Reconcile+push #N' directive to evaluate.",
    ),
    EnvOverlayCanary(
        flag="refutable_standdown_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 135 refutable stand-down canary (TOML [loop] "
        "refutable_standdown_enabled); criteria in the 'Refutable stand-down "
        "canary (proposal 135)' subsection of docs/self-evolution.md; deployed by "
        "this PR's production merge with a one-production-dream-window observation "
        "period. standdown_refutation_release_cap inherits its base value. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: a handoff-ready latch "
        "can only be refuted where real sibling PRs merge against a real "
        "origin/main and flip an owned PR's live owed act to CODE_OWED "
        "(production) — the standdown_refuted gate rows, the [standdown-refuted] "
        "PR-LAPS clause and measure_contradicted_standdowns all fold over live "
        "host/ledger state, so a dark-launched off-everywhere build would gather "
        "no release evidence; flag-off is byte-identical, and the deadlock this "
        "releases (a stood-down CONFLICTING PR starving the task queue) is itself "
        "a production-only failure.",
    ),
    EnvOverlayCanary(
        flag="prestamp_merge_truth_gate_enabled",
        env="production",
        review_by="2026-12-31",
        note="issue #3280 Tier 2 landing-credit grounding canary (TOML [loop] "
        "prestamp_merge_truth_gate_enabled, proposal 087); criteria in the "
        "'Landing-credit grounding canary (issue #3280)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge. Paired "
        "with the two Tier 1 [audit] canaries so both directions of the "
        "credit/over-claim axis move together.",
        justification="Observability, not a feature-gate: an over-claimed success "
        "naming #N can only be caught against a live is_pr_settled read of a real "
        "PR (production) — the write-time downgrade's provenance stamps and the "
        "rule-14 finding rate it is meant to displace are both production-only "
        "measurements, so a dark-launched off-everywhere build would gather no "
        "over-claim evidence.",
    ),
    EnvOverlayCanary(
        flag="sdk_single_use_evolution_calls_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 139 single-use evolution calls canary (TOML [sdk] "
        "single_use_evolution_calls_enabled); criteria in the 'Single-use "
        "evolution calls canary (proposal 139)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        ">=14-day observation window. Also in EXPERIMENTAL_FLAGS on the base "
        "default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the chained-curate "
        "self-refine rate and the per-caller context/cost figures come only from "
        "real evolve -> curate -> dream chains, which run only where [loop] "
        "curate_enabled is on (a PROD_ONLY_OPERATIONAL divergence) — so a "
        "dark-launched off-everywhere build would gather no evidence; flag-off is "
        "byte-identical to today's shared slot-B session.",
    ),
    EnvOverlayCanary(
        flag="cooldown_signature_keying_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 137 owed-act chokepoint canary (TOML [loop] "
        "cooldown_signature_keying_enabled); criteria in the 'Owed-act chokepoint "
        "canary (proposal 137)' subsection of docs/self-evolution.md; deployed by "
        "this PR's production merge. Also in EXPERIMENTAL_FLAGS on the base "
        "default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: a cooldown only bounces "
        "against real PR heads, a real origin/main and real CI rollups, and the "
        "tend_cooldown_skip rows and OWED-ACT-DIVERGENCE sensor fold over live host "
        "state (production) — so a dark-launched off-everywhere build would gather "
        "no evidence; flag-off is byte-identical to today's wall-clock windows.",
    ),
    EnvOverlayCanary(
        flag="owed_act_binding_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 137 owed-act chokepoint canary (TOML [loop] "
        "owed_act_binding_enabled); criteria in the 'Owed-act chokepoint canary "
        "(proposal 137)' subsection of docs/self-evolution.md; deployed by this "
        "PR's production merge. Also in EXPERIMENTAL_FLAGS on the base default-off "
        "axis (distinct concern).",
        justification="Observability, not a feature-gate: rule 21's close clause, "
        "the salvage veto and the WIP focus (a code-owed own PR, even red-only, "
        "claims the WIP-primacy slot and blocks discretionary generation) only "
        "act on real open own PRs with measured readiness (production), and the binding costs at most one "
        "memoized readiness observation per cited PR — so a dark-launched "
        "off-everywhere build would gather no evidence.",
    ),
    EnvOverlayCanary(
        flag="gate_environment_oracle_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 133 gate-runnability oracle canary (TOML [loop] "
        "gate_environment_oracle_enabled); criteria in the 'Gate-runnability "
        "oracle canary (proposal 133)' subsection of docs/self-evolution.md; "
        "deployed by this PR's production merge with a >=14-day observation "
        "window. Also in EXPERIMENTAL_FLAGS on the base default-off axis "
        "(distinct concern).",
        justification="Observability, not a feature-gate: the runnability "
        "verdict measures the environment the loop's own gate runs in — the "
        "production container image whose missing maestro_sdk install aborted "
        "gate collection (RCA #3872) — and its evidence (declined gate_runs "
        "admissions, gate_environment_hold rows, approved plans held instead of "
        "demoted) accrues only where the live loop implements human-approved "
        "plans, so a dark-launched off-everywhere build would gather none; "
        "flag-off is byte-identical.",
    ),
    EnvOverlayCanary(
        flag="self_review_own_session_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 140 declared-effort canary (TOML [loop] "
        "self_review_own_session_enabled); criteria in the 'Declared-effort "
        "canary (proposal 140)' subsection of docs/self-evolution.md; deployed by "
        "this PR's production merge with a >=7-day observation window. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the independent-review "
        "readings (first-round LGTM rate, zero-tool-call LGTM share, self-review "
        "context p50) fold only over real implement -> self_review lineages, "
        "which run only in the production loop, so a dark-launched "
        "off-everywhere build would gather no evidence; flag-off is "
        "byte-identical to today's review turn on the implementer's session.",
    ),
    EnvOverlayCanary(
        flag="sdk_call_policy_resolution_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 140 declared-effort canary (TOML [sdk] "
        "call_policy_resolution_enabled); criteria in the 'Declared-effort "
        "canary (proposal 140)' subsection of docs/self-evolution.md; deployed by "
        "this PR's production merge with a >=7-day observation window. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the per-caller tier "
        "effort on cost_samples.jsonl rows and the implementation / planning "
        "tripwires (failed/abandoned attempt share, auto_fix rounds, rejection "
        "rate) fold only over the full evolve chain, which runs only in the "
        "production loop; the seam's mismatch raise also needs the prod-only "
        "single-use evolution calls canary on, so a dark-launched off-everywhere "
        "build would gather no evidence; flag-off is byte-identical.",
    ),
    EnvOverlayCanary(
        flag="sdk_enforced_tool_surface_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 142 enforced tool-surface canary (TOML [sdk] "
        "enforced_tool_surface_enabled); criteria in the 'Enforced tool-surface "
        "canary (proposal 142)' subsection of docs/self-evolution.md; deployed by "
        "this PR's production merge with a >=14-day observation window. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the off-surface and "
        "default-toolbelt figures maestro tool-surface-audit reports fold only "
        "over real implement, auto_fix, self_review, execute_step and "
        "respond_to_messages sidecars, which run only in the production loop, so "
        "a dark-launched off-everywhere build would gather no evidence; flag-off "
        "is byte-identical.",
    ),
    EnvOverlayCanary(
        flag="gate_budget_lease_owned_enabled",
        env="production",
        review_by="2026-12-31",
        note="proposal 134 gate-fit budget canary (TOML [loop] "
        "gate_budget_lease_owned_enabled), the first-stage deadline flag; the "
        "reserve and impacted-scope flags stay dark for later windows. Criteria in "
        "the 'Gate-fit budget canary (proposal 134)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        ">=14-day observation window. Also in EXPERIMENTAL_FLAGS on the base "
        "default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the derived deadline "
        "folds over gate_runs.jsonl rows at the width the lease grants, and the "
        "fit it is measured against (TIMEOUT share, censored width buckets, "
        "completed-run p50 vs derived deadline in measure-gate-contention) exists "
        "only where the live loop runs its own gate inside the cgroup-bounded "
        "production container implementing human-approved plans, so a "
        "dark-launched off-everywhere build would gather no evidence; flag-off is "
        "byte-identical to today's flat selfcheck deadline.",
    ),
    EnvOverlayCanary(
        flag="host_capacity_sensor_enabled",
        env="production",
        review_by="2027-01-31",
        note="proposal 125 host-capacity sensor canary (TOML [loop] "
        "host_capacity_sensor_enabled). Criteria in the 'Host-capacity sensor "
        "canary (proposal 125)' subsection of docs/self-evolution.md; deployed by "
        "this PR's production merge with a 14-day window spanning >=2 real "
        "deploys. Also in EXPERIMENTAL_FLAGS on the base default-off axis "
        "(distinct concern).",
        justification="Observability, not a feature-gate: the saturation the "
        "sensor calibrates against — safe-deploy.sh build-cache disk churn and "
        "stop/rebuild swap pressure — happens only on the production host, since "
        "CI runs off the production host and dev has no deploy window. "
        "Off elsewhere the health summary stays byte-identical.",
    ),
    EnvOverlayCanary(
        flag="grounded_planning_enabled",
        env="production",
        review_by="2027-01-31",
        note="proposal 143 grounded planning canary (TOML [loop] "
        "grounded_planning_enabled), on propose_plan at the "
        "grounded_planning_callers default; propose_plan_chain and "
        "curate_pending_plans stay off the list for later windows. Criteria in "
        "the 'Grounded planning canary (proposal 143)' subsection of "
        "docs/self-evolution.md; deployed by this PR's production merge with a "
        "window of >=30 human-decided plans minted after the flip. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the success figure is "
        "the tree-checkable share of human plan rejections, which accrues only "
        "where Thomas decides plans the live loop mints (production), and the "
        "flag requires the prod-only single-use evolution calls canary, so a "
        "dark-launched off-everywhere build could neither run the grounded "
        "planner nor gather rejection, grounded_reads or [planner-read] "
        "door-refusal evidence; flag-off is byte-identical to today's tool-free "
        "planner.",
    ),
    EnvOverlayCanary(
        flag="grounded_planning_rejection_tool_enabled",
        env="production",
        review_by="2027-01-31",
        note="grounded planner rejection-ledger search canary (TOML [loop] "
        "grounded_planning_rejection_tool_enabled). It adds search_rejections, a "
        "sixth pure tool, to the grounded planner's bounded tree surface so the "
        "planner queries the specific ideas already refused (and their reasons) "
        "at mint time. Deployed by this PR's production merge; success is a fall "
        "in rejection:duplicate / rejection:wrong_approach among plans minted "
        "after the flip. Also in EXPERIMENTAL_FLAGS on the base default-off axis "
        "(distinct concern).",
        justification="Observability, not a feature-gate: the figure it moves — "
        "the duplicate / wrong-approach human rejection rate — accrues only where "
        "Thomas decides plans the live loop mints (production), and the tool only "
        "serves when the prod-only grounded planning canary is already on, so a "
        "dark-launched off-everywhere build could neither carry the tool nor "
        "gather the rejection evidence; flag-off is byte-identical to the "
        "five-tool grounded planner.",
    ),
    EnvOverlayCanary(
        flag="loop_plan_feedback_enabled",
        env="production",
        review_by="2027-01-31",
        note="proposal 145 reviewer feedback canary (TOML [loop] "
        "plan_feedback_enabled). Criteria in the 'Reviewer feedback canary "
        "(proposal 145)' subsection of docs/self-evolution.md; deployed by this "
        "PR's production merge with a window of >=30 human-decided plans. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the flag acts only "
        "inside the curate_pending_plans phase, which runs only where the "
        "PROD_ONLY_OPERATIONAL curate_enabled switch is on (production), and its "
        "evidence — the approval rate of feedback-refined plans and the "
        "three-strike release share — accrues only where Thomas leaves feedback "
        "on and decides plans the live loop mints, so a dark-launched "
        "off-everywhere build would gather none; flag-off is byte-identical to "
        "today's curator.",
    ),
    EnvOverlayCanary(
        flag="generator_reality_grounding_enabled",
        env="production",
        review_by="2027-01-31",
        note="proposal 136 generator reality grounding canary (TOML [loop] "
        "generator_reality_grounding_enabled). Criteria in the 'Generator reality "
        "grounding canary (proposal 136)' subsection of docs/self-evolution.md; "
        "deployed by this PR's production merge with a window of >=30 "
        "human-decided plans, shared with proposal 143's. Also in "
        "EXPERIMENTAL_FLAGS on the base default-off axis (distinct concern).",
        justification="Observability, not a feature-gate: the falsification "
        "figures — the already-shipped / false-premise / not-needed rejection "
        "classes and the content-death share of measure-dead-on-arrival-mints — "
        "accrue only where Thomas decides plans the live loop mints, and the "
        "planner reads the measured-problem ids it must cite only through the "
        "prod-only grounded planning canary's read_reality_inventory tool, so a "
        "dark-launched off-everywhere build would gather no evidence; flag-off is "
        "byte-identical to today's door.",
    ),
)


#: Today's prod-only canary flag-set, frozen 2026-08-24 as a dated, grandfathered
#: baseline. These five flags were enabled prod-only before this increment demoted
#: environment divergence as a *feature-gating* tool, so each carries an empty
#: :attr:`EnvOverlayCanary.justification` — they are grandfathered, not justified.
#: Any *new* :data:`PROD_ONLY_CANARIES` entry (a flag **not** in this baseline) must
#: name an explicit observability/operational ``justification``, enforced by
#: :func:`find_unjustified_canaries` and its CI guard
#: (``test_new_prod_only_canary_carries_justification``): a new prod-only enable
#: added without one reds the build, steering a new feature toward the disciplined
#: default — dark-launch off-everywhere via :data:`EXPERIMENTAL_FLAGS`, not an
#: environment divergence. Self-cleaning: ``test_prod_only_canary_baseline_is_current``
#: reds if a baseline flag graduates or is removed from :data:`PROD_ONLY_CANARIES`,
#: forcing this set to shrink with the manifest so it never grandfathers a flag
#: that is gone.
PROD_ONLY_CANARY_BASELINE: frozenset[str] = frozenset(
    {
        "context7_mcp_enabled",
        "phantom_escalation_delivery_enabled",
        "unconfirmed_landing_forced_enabled",
        "audit_reality_refutation_display_enabled",
        "audit_reality_refutation_enabled",
    },
)


#: Permanent prod-only operational/topology divergences: enabled in
#: ``maestro.production.toml`` **by design** with **no** graduation criterion and
#: **no** ``review_by``, because they must never converge to base (dev/local have no
#: equivalent). Recorded here so the drift guard can distinguish them from accidental
#: undeclared enables, but never overdue-checked — forcing a horizon on them would be
#: self-defeating. Disjoint from :data:`PROD_ONLY_CANARIES` by construction.
PROD_ONLY_OPERATIONAL: tuple[EnvOverlayDivergence, ...] = (
    EnvOverlayDivergence(
        flag="trust_forwarded_for",
        env="production",
        note="permanent: nginx is the sole trusted hop in production and sets "
        "X-Real-IP; dev/local have no reverse proxy, so base stays off. Enabled "
        "2026-04-24 (05203c9b). No graduation horizon — must stay diverged.",
    ),
    EnvOverlayDivergence(
        flag="loop_curate_enabled",
        env="production",
        note="permanent: autonomous plan-curation phase (TOML [loop] "
        "curate_enabled) enabled in production only by operational choice; base "
        "off. Enabled 2026-06-12 (2d6a6287). No graduation horizon — must stay "
        "diverged.",
    ),
)


#: Prod-only **disabled** flags: on by default in base ``maestro.toml`` and flipped
#: ``false`` in ``maestro.production.toml`` — the subtractive mirror of
#: :data:`PROD_ONLY_CANARIES`, computed by :func:`disabled_only_in_overlay` and
#: bound by ``test_prod_only_disabled_flags_are_all_declared``. Every entry is a
#: deliberate production dormancy lever or kill-switch, and every one is *meant* to
#: converge: either the burn-in window closes and the retirement/graduation PR lands
#: (removing the override and the entry together), or the override is reverted. The
#: dated ``review_by`` is that forcing function — without it an accidental
#: prod-disable, or a canary left diverged long after its observation window should
#: have closed, has nothing forcing a decision. Reuses :class:`EnvOverlayCanary`
#: rather than a parallel record so the horizon/justification contract is shared;
#: disjoint from :data:`PROD_ONLY_CANARIES` and :data:`PROD_ONLY_OPERATIONAL` by
#: construction. Note that ``tend_main_merge_enabled``
#: also appears in :data:`CUTOVER_FLAGS` — that axis
#: tracks the *base* default-on to collapse lifecycle, a distinct concern from this
#: overlay divergence and deliberately not conflated with it.
PROD_ONLY_DISABLED: tuple[EnvOverlayCanary, ...] = (
    EnvOverlayCanary(
        flag="branch_promotion_pressure_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 116 branch-promotion-pressure burn-in; criteria in the "
        "'Plan-mill closure canary (proposal 116)' subsection of "
        "docs/self-evolution.md; deployed 2026-08-21 (dab967dd) with a "
        "one-production-dream-window observation period — same horizon as the "
        "additive 116 canaries. The subsystem's deletion is prepared on a feature "
        "branch and must not merge until that window confirms graduation.",
        justification="The graduation decision rests on measuring the production "
        "loop's behaviour with the subsystem inert; no other environment runs the "
        "autonomous loop long enough to produce that evidence.",
    ),
    EnvOverlayCanary(
        flag="tend_main_merge_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 128 merge-queue kill-switch; criteria and tripwires in the "
        "'Cutover & observation' note under the Merge Queue section of "
        "docs/ci.md; deployed 2026-09-10 (e88e0226) with a bounded tripwire "
        "window that has since closed cleanly, leaving the flag itself as a later "
        "retirement increment. Also in CUTOVER_FLAGS on the base default-on axis "
        "(distinct concern).",
        justification="A kill-switch is only load-bearing where the risk is — the "
        "production PR-tending loop — and must stay reachable there without "
        "disabling the merge path for tests and dev.",
    ),
    EnvOverlayCanary(
        flag="task_branch_reuse_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 116 plan-mill closure lever: production-only suppression of "
        "kept-task-branch reuse as an evolution-PR base, so evolution PRs cut "
        "fresh from main; criteria in the 'Plan-mill closure canary (proposal "
        "116)' subsection of docs/self-evolution.md; deployed 2026-08-21 "
        "(dab967dd) with a one-production-dream-window observation period — same "
        "horizon as the additive 116 canaries.",
        justification="Branch reuse only has real conflict surface against the "
        "production repository's live branch set, so the suppression has to be "
        "scoped to production rather than turned off everywhere.",
    ),
    EnvOverlayCanary(
        flag="self_directed_tasks_enabled",
        env="production",
        review_by="2026-10-31",
        note="proposal 116 plan-mill closure lever: production-only hold on "
        "self-directed task generation, keeping the production queue driven by "
        "human and PR-remediation requests; criteria in the 'Plan-mill closure "
        "canary (proposal 116)' subsection of docs/self-evolution.md; deployed "
        "2026-08-21 (dab967dd) with a one-production-dream-window observation "
        "period — the horizon the additive 116 canaries graduated under.",
        justification="Only the production loop mints self-directed tasks against "
        "the real repository, so the hold must be a production override rather "
        "than a base default that would disable the path in tests and dev.",
    ),
)


def parse_review_by(value: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` ``review_by`` string into a :class:`date`.

    Fails loud on a malformed date (matching the fail-loud invariant): a typo in a
    manifest entry surfaces as a clear ``ValueError`` rather than silently sorting
    as never-overdue.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Invalid review_by date {value!r}: expected ISO YYYY-MM-DD",
        ) from exc


#: Flip-intent vocabulary for the flag-graduation grounding door — the verbs a
#: self-initiated plan uses when it proposes to *cut over* an experimental flag
#: (turn it on by default / graduate / promote it) rather than merely mention it.
#: Three groups of arms count as flip intent:
#:
#: * imperative verbs — ``flip``/``graduate``/``promote``/``enable``/
#:   ``activate`` (base, past and ``-ing`` forms; the descriptive third-person
#:   ``enables``/``activates``/``flips`` and the participles ``enabled``/
#:   ``activated`` do not count), ``turn ... on``/``switch ... on`` with at most
#:   one token between, and ``set <key> true``;
#: * default claims — ``default-on``, ``on|enabled by default`` and
#:   ``default (... ) to enabled|on|true`` with at most two tokens between;
#: * assign-true — ``= true``, ``to true``, ``-> true`` and a TOML/YAML
#:   ``key: true`` (the colon must follow a snake_case key, a backtick or a
#:   ``[section] key``, so prose ``Note: true`` does not count),
#:   skipping comparisons (``== true``, ``!= true``, ``<= true``) and the
#:   descriptive ``evaluates to true`` / ``resolves to true``.
#:
#: An ``on`` arm never matches a hyphenated word (``on-call``), and ``turns on``
#: stays descriptive. A bare mention without one of these in the same clause is
#: not a cutover attempt, so the door admits it (the human-owned graduation path
#: is only pre-empted by an explicit flip proposal). A clause that also carries
#: turn-off wording (``_FLAG_OFF_INTENT_RE``) is vetoed and admitted as well.
_FLAG_FLIP_INTENT_RE = re.compile(
    r"\b(?:flip(?:ped|ping)?|graduat(?:e|es|ed|ing)|promot(?:e|es|ed|ing)"
    r"|default[- ]on|(?:on|enabled)\s+by\s+default|enabl(?:e|ing)"
    r"|turn(?:ing)?\s+(?:[\w`\[\]]+\s+)?on"
    r"|set\s+(?:[\w`\[\]]+\s+){1,2}?true"
    r"|default(?:s|ed)?\s+(?:(?:[\w`\[\]]+\s+){0,2}?to\s+)?(?:enabled|on|true)"
    r"|switch(?:es|ing)?\s+(?:[\w`\[\]]+\s+)?on|activat(?:e|ing))(?![\w-])"
    r"|(?:(?<![!=<>])=(?!=)|(?<!evaluates\s)(?<!resolves\s)\bto|->"
    r"|(?:\w_\w*|\]\s*\w+|`)\s*:)"
    r"\s*`?\s*true\b",
    re.IGNORECASE,
)

#: Turn-off wording that vetoes a clause's flip intent: a rollback such as
#: "change `X = true` to `false`" or "Disable X (currently = true)" names the
#: flag's *current* ``= true`` value, which must not read as a cutover.
_FLAG_OFF_INTENT_RE = re.compile(
    r"\b(?:disabl(?:e|es|ed|ing)|turn(?:s|ed|ing)?\s+off)\b"
    r"|\bto\s*`?\s*false\b",
    re.IGNORECASE,
)

#: Source paths whose change marks a plan as touching the flag-cutover boundary:
#: the TOML defaults, the ``Config`` dataclass, or this manifest. A plan that
#: names an experimental flag without touching one of these is not proposing a
#: cutover and is admitted.
_FLAG_CUTOVER_PATHS: tuple[str, ...] = (
    "maestro.toml",
    "maestro/config.py",
    "maestro/flag_lifecycle.py",
)

#: Clause splitter for the graduation door. Every :data:`_FLAG_CUTOVER_PATHS`
#: entry carries a dot, so a path or extension dot must not cut a clause: a
#: ``.`` splits only when it ends a sentence (followed by whitespace or end of
#: string); newlines and semicolons always split.
_CLAUSE_SPLIT_RE = re.compile(r"\.(?!\S)|[\n;]+")


@functools.cache
def _toml_location_by_field() -> dict[str, tuple[str, str]]:
    """Map each ``Config`` field to its ``(section, TOML key)`` via ``Field.alias``.

    Imported lazily so this module stays a leaf with no import cycle; the
    section models are the authoritative field -> TOML key mapping.
    """
    from maestro.config_settings import _SECTION_MODELS

    return {
        name: (section, info.alias or name)
        for section, model in _SECTION_MODELS.items()
        for name, info in model.model_fields.items()
    }


def _flag_token_forms(flag: str) -> tuple[str, ...]:
    """Return the code and ``maestro.toml`` surface forms of a flag key.

    The forms are the ``Config`` field name verbatim
    (``loop_curate_skip_serviced_enabled``), the bare ``maestro.toml`` key
    (``curate_skip_serviced_enabled``) and its ``[section] key`` header form
    (``[loop] curate_skip_serviced_enabled``), the last two resolved from the
    field's authoritative ``Field.alias`` in
    ``maestro.config_settings._SECTION_MODELS`` — never by splitting the field
    name at its first underscore. A key with no ``Config`` field has only its
    verbatim form. Duplicates are dropped.
    """
    forms = [flag]
    location = _toml_location_by_field().get(flag)
    if location is not None:
        section, alias = location
        # A bare alias without an underscore (``enabled``) is a generic word,
        # not a distinctive key; only its ``[section]`` form identifies the flag.
        bare = (alias,) if "_" in alias else ()
        for form in (*bare, f"[{section}] {alias}"):
            if form not in forms:
                forms.append(form)
    return tuple(forms)


@functools.cache
def _flag_token_re(flag: str) -> re.Pattern[str]:
    """Compile an identifier-bounded alternation of :func:`_flag_token_forms`."""
    forms = sorted(_flag_token_forms(flag), key=len, reverse=True)
    alternation = "|".join(re.escape(form) for form in forms)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


class _GraduationPlan(Protocol):
    """Structural view of the plan fields the graduation door reads.

    A Protocol (not an ``EvolutionPlan`` import) keeps this module free of a
    circular dependency on ``maestro.memory`` — the door reads only the title,
    the proposed-change clauses, and the affected paths.
    """

    @property
    def title(self) -> str: ...

    @property
    def proposed_changes(self) -> Sequence[str]: ...

    @property
    def files_affected(self) -> Sequence[str]: ...


def flag_graduation_grounding_verdict(
    plan: _GraduationPlan, today: date,
) -> str | None:
    """Return the ``EXPERIMENTAL_FLAGS`` key a plan prematurely graduates, or None.

    A human-owned cutover the loop must not pre-empt: flipping/graduating a
    default-off experimental flag before its ``review_by`` horizon closes. The
    door returns the offending flag key only when **all** hold: the plan's
    ``files_affected`` touch one of :data:`_FLAG_CUTOVER_PATHS`; a title or
    ``proposed_changes`` clause names an ``EXPERIMENTAL_FLAGS`` key (its code
    form, bare TOML key or ``[section] key`` form, via :func:`_flag_token_forms`,
    matched at identifier boundaries) *and* carries flip intent
    (:data:`_FLAG_FLIP_INTENT_RE`) in that same clause; and the flag's
    ``review_by`` is still in the future (not overdue) by :func:`parse_review_by`
    against the caller-supplied *today*. Else ``None`` — a past-``review_by``
    flag (the post-window graduation is the human's to make), a bare mention
    without flip intent, no flag token, or a plan that touches none of the
    cutover paths all pass. Clock-free (``today`` is a parameter) and zero-cost;
    the caller scopes it to self-initiated plans. Clauses are cut at
    sentence-ending dots, newlines and semicolons (:data:`_CLAUSE_SPLIT_RE`),
    never at a dot inside a path token such as ``maestro.toml``. A clause that
    also carries turn-off wording (:data:`_FLAG_OFF_INTENT_RE`) is vetoed, so a
    rollback naming the flag's current ``= true`` value is not a cutover.
    """
    if not any(
        any(path in affected for path in _FLAG_CUTOVER_PATHS)
        for affected in plan.files_affected
    ):
        return None
    clauses = [
        clause
        for part in (plan.title, *plan.proposed_changes)
        for clause in _CLAUSE_SPLIT_RE.split(part)
        if _FLAG_FLIP_INTENT_RE.search(clause)
        and not _FLAG_OFF_INTENT_RE.search(clause)
    ]
    for entry in EXPERIMENTAL_FLAGS:
        if parse_review_by(entry.review_by) < today:
            # Window closed — a post-horizon graduation is the human's call.
            continue
        token_re = _flag_token_re(entry.flag)
        if any(token_re.search(clause) for clause in clauses):
            return entry.flag
    return None


class _HasReviewBy(Protocol):
    """Structural type for any manifest record carrying an ISO ``review_by``.

    Declared read-only (a property, not a plain attribute) so a *frozen* dataclass
    — every manifest record here is one — satisfies it; a mutable-attribute protocol
    would reject the frozen field's read-only variance.
    """

    @property
    def review_by(self) -> str: ...


def find_overdue_flags[FlagT: _HasReviewBy](
    flags: Iterable[FlagT], today: date,
) -> list[FlagT]:
    """Return the entries whose ``review_by`` is strictly before *today*.

    Deterministic: *today* is supplied by the caller, so the horizon comparison
    never reads the wall clock inside the helper. Generic over any record exposing
    a ``review_by`` string (:class:`ExperimentalFlag` and :class:`EnvOverlayCanary`
    both do), so the base-axis and overlay-axis staleness guards share one horizon
    check — :data:`PROD_ONLY_OPERATIONAL` carries no ``review_by`` and is never
    passed here.
    """
    return [f for f in flags if parse_review_by(f.review_by) < today]


#: How many days ahead of a ``review_by`` horizon the push-side sensor starts
#: naming a flag — a monthly-cadence heads-up, so an approaching promote-or-delete
#: deadline surfaces in contemplation before it reds the build. Kept a named
#: constant (not a bare ``30`` inside the selector) so the approach window lives in
#: one reviewable place, mirroring :data:`_MAX_BURN_IN_DIVERGENCES`.
_HORIZON_APPROACH_DAYS = 30


def find_horizons_within[FlagT: _HasReviewBy](
    flags: Iterable[FlagT], today: date, within_days: int = _HORIZON_APPROACH_DAYS,
) -> list[FlagT]:
    """Return the entries whose ``review_by`` is within *within_days* of *today*.

    The push-side analogue of :func:`find_overdue_flags`: where the CI guard fires
    only *after* a horizon is crossed, this names a horizon that is merely
    approaching, so the agent can weigh it during contemplation. An
    already-overdue entry (``review_by`` before *today*, a negative day-delta) is
    ``<= within_days`` too, so it is naturally included — the caller distinguishes
    approaching from past. Deterministic and clock-free by the same contract as its
    sibling: *today* is supplied by the caller, never read from the wall clock, and
    it is generic over any record exposing a ``review_by`` string.
    """
    horizon = timedelta(days=within_days)
    return [
        f for f in flags if parse_review_by(f.review_by) - today <= horizon
    ]


def format_flag_horizon_summary(
    flags: Iterable[ExperimentalFlag],
    today: date,
    within_days: int = _HORIZON_APPROACH_DAYS,
) -> str:
    """Render a single self-suppressing ``FLAG-HORIZON`` line for due flags.

    A zero-cost measured-data sensor for ``contemplate`` (the push-side, agent-facing
    analogue of the pull-only :func:`audit_flags_report`): selects the flags within
    (or past) their ``review_by`` via :func:`find_horizons_within`, and names each
    with its horizon date, marking the already-past ones ``OVERDUE`` and the merely
    approaching ones with the remaining day-count. Returns ``""`` when no flag is
    near or past its horizon — the standard self-suppression guard, so an empty
    window adds no line. Pure formatting, clock-free (*today* supplied by the
    caller), no SDK cost.
    """
    due = find_horizons_within(flags, today, within_days)
    if not due:
        return ""
    parts: list[str] = []
    for f in sorted(due, key=lambda flag: (flag.review_by, flag.flag)):
        horizon = parse_review_by(f.review_by)
        if horizon < today:
            parts.append(f"{f.flag} OVERDUE (was {f.review_by})")
        else:
            days_left = (horizon - today).days
            parts.append(f"{f.flag} due {f.review_by} ({days_left}d)")
    return "FLAG-HORIZON: " + "; ".join(parts)


def enabled_only_in_overlay(
    base_flat: Mapping[str, object], overlay_flat: Mapping[str, object],
) -> set[str]:
    """Return the flattened field names enabled (boolean ``True``) only in the overlay.

    Pure over two already-flattened, ``Config``-field-keyed dicts — a built base
    ``Config``'s fields vs. a named environment overlay's — so the real-file CI
    guard and the seeded negative controls share one definition of
    "prod-only-enabled". A key is prod-only-enabled when its
    value is boolean ``True`` in the merged overlay but **not** boolean ``True`` in
    base (off, absent, or any non-``True`` value). Comparing ``is True`` rather than
    truthiness keeps a numeric override like ``contemplate_every = 5`` — which is not
    a feature-flag enable — correctly out of scope.
    """
    return {
        key
        for key, value in overlay_flat.items()
        if value is True and base_flat.get(key) is not True
    }


def disabled_only_in_overlay(
    base_flat: Mapping[str, object], overlay_flat: Mapping[str, object],
) -> set[str]:
    """Return the flattened field names enabled in base but **not** in the overlay.

    The subtractive mirror of :func:`enabled_only_in_overlay`, over the same two
    already-flattened, ``Config``-field-keyed dicts, so the real-file CI guard and
    the seeded negative controls share one definition of "prod-only-disabled". A key
    is prod-only-disabled when its value is boolean ``True`` in base but **not**
    boolean ``True`` in the merged overlay (off, absent, or any non-``True`` value)
    — a base default-on behaviour the overlay turns off, the polarity a burn-in
    dormancy lever or a production kill-switch takes. Comparing ``is True`` on both
    sides rather than truthiness keeps a numeric override like
    ``contemplate_every = 5`` — which is not a feature-flag disable — correctly out
    of scope, exactly as the additive sibling does.
    """
    return {
        key
        for key, value in base_flat.items()
        if value is True and overlay_flat.get(key) is not True
    }


_PROPOSAL_REF = re.compile(r"proposal (\d{3})")


def overlay_tag(kind: str, value: bool, note: str) -> str:
    """Render the ``[production: …]`` tag a diverged base ``maestro.toml`` key carries.

    ``maestro.toml`` holds base defaults only, so a reader of a base ``false`` line
    cannot tell production runs the flag on. The tag names the production *value*
    and its provenance — ``kind`` (``canary`` / ``prod-disabled`` / ``operational``)
    plus the first ``proposal NNN`` cited in ``note``, omitted when ``note`` cites
    none. Derived from the manifests, never hand-authored.
    """
    match = _PROPOSAL_REF.search(note)
    provenance = f"{kind}, proposal {match.group(1)}" if match else kind
    return f"[production: {'true' if value else 'false'} — {provenance}]"


def expected_overlay_tags() -> dict[str, str]:
    """Return the tag every production-diverged base key must carry, by ``Config`` field.

    Maps :data:`PROD_ONLY_CANARIES` → ``canary``/``true``, :data:`PROD_ONLY_DISABLED`
    → ``prod-disabled``/``false`` and :data:`PROD_ONLY_OPERATIONAL` →
    ``operational``/``true``; the three manifests are disjoint by construction.
    """
    tags = {c.flag: overlay_tag("canary", True, c.note) for c in PROD_ONLY_CANARIES}
    tags.update(
        {d.flag: overlay_tag("prod-disabled", False, d.note) for d in PROD_ONLY_DISABLED}
    )
    tags.update(
        {o.flag: overlay_tag("operational", True, o.note) for o in PROD_ONLY_OPERATIONAL}
    )
    return tags


def find_stage_violations(
    flags: Iterable[CutoverFlag], defined_symbols: set[str],
) -> list[str]:
    """Return one message per manifest entry breaking the stage<->off-path rule.

    Pure over its two arguments — the manifest and the set of symbol names defined
    under ``maestro/`` (from :func:`tests.support.structural_guard.symbols_defined_in`)
    — so the real-tree guard and the negative controls share one definition. It
    mirrors :func:`find_overdue_flags`'s clock-free/pure shape. Three ways an entry
    can be inconsistent:

    * a non-``collapsed`` entry (``cutover``, ``retain``, or ``collapse_eligible``)
      whose ``off_path_symbol`` is **absent** — the off-path was retired but the entry
      was left behind (the subject-existence self-prune trigger, borrowing 109's
      guard-registry idiom). For ``collapse_eligible`` this is a completed collapse
      that left its manifest marker behind: the normal flow deletes the symbol *and*
      removes the entry in one PR, so ``collapse_eligible``+absent never legitimately
      arises and must red the build the same way ``cutover``/``retain``+absent does;
    * a ``collapse_eligible`` entry whose ``off_path_symbol`` is **still present** — a
      collapse was declared but never finished (the anti-indefinite-retention
      trigger);
    * **any** ``collapsed`` entry — a completed collapse removes the entry rather than
      persisting the marker, so both a claimed-but-not-done ``collapsed`` (symbol
      present) and a leftover-tombstone ``collapsed`` (symbol absent) fail.
    """
    violations: list[str] = []
    for f in flags:
        present = f.off_path_symbol in defined_symbols
        if f.stage == "collapsed":
            violations.append(
                f"{f.flag}: stage 'collapsed' but the entry is still in the "
                "manifest — a completed collapse removes the entry rather than "
                "persisting the marker.",
            )
        elif not present:
            violations.append(
                f"{f.flag}: stage {f.stage!r} but off_path_symbol "
                f"{f.off_path_symbol!r} is no longer defined under maestro/ — the "
                "off-path was retired; remove this entry.",
            )
        elif f.stage == "collapse_eligible":
            violations.append(
                f"{f.flag}: stage 'collapse_eligible' but off_path_symbol "
                f"{f.off_path_symbol!r} is still defined under maestro/ — the "
                "collapse was declared but not finished.",
            )
    return violations


def find_unjustified_canaries(
    canaries: Iterable[EnvOverlayCanary], baseline: frozenset[str],
) -> list[str]:
    """Return the sorted non-baseline canary flags carrying no justification.

    Pure over its two arguments — the :data:`PROD_ONLY_CANARIES` manifest and the
    frozen :data:`PROD_ONLY_CANARY_BASELINE` set — so the real-tree guard and the
    seeded negative control share one definition, mirroring
    :func:`find_stage_violations`'s clock-free, pure-over-its-arguments shape. A
    canary is *unjustified* when it is **not** in *baseline* (so it was added after
    the freeze) **and** its ``justification`` is empty once stripped — a new
    prod-only enable that named no observability/operational reason. Grandfathered
    baseline entries are exempt even with an empty justification, and any
    non-baseline entry carrying a non-empty justification passes.
    """
    return sorted(
        c.flag
        for c in canaries
        if c.flag not in baseline and not c.justification.strip()
    )


def audit_flags_report(
    flags: Iterable[ExperimentalFlag],
    activation_counts: Mapping[str, int],
    today: date,
) -> list[dict[str, object]]:
    """Join the manifest against pre-computed activation counts into rows.

    Each :class:`ExperimentalFlag` maps to a row: ``flag``, ``review_by``,
    ``overdue`` (from :func:`find_overdue_flags`), ``activations``, and ``note``
    (the manifest provenance line, so it is reachable via the ``--json`` read
    surface, not just in source). ``activations`` is ``activation_counts.get(gate,
    0)`` when ``gate`` is set — a gate-linked flag that never fired renders ``0``,
    distinct from the ``None`` a no-gate flag carries, so a zero fire-rate stays a
    live promote-or-delete signal rather than collapsing into "no trace". This is
    the single shape both the human table and the ``--json`` payload render from;
    it stays clock- and I/O-free because the CLI supplies both *activation_counts*
    and *today*.
    """
    flag_list = list(flags)
    overdue = {f.flag for f in find_overdue_flags(flag_list, today)}
    rows: list[dict[str, object]] = []
    for f in flag_list:
        rows.append(
            {
                "flag": f.flag,
                "review_by": f.review_by,
                "overdue": f.flag in overdue,
                "activations": (
                    activation_counts.get(f.gate, 0)
                    if f.gate is not None
                    else None
                ),
                "note": f.note,
            },
        )
    return rows


#: A clean cut-over burn-in requires zero observed off-path activations: any
#: divergence during burn-in is worth a human look before a collapse. Kept a
#: named constant (not a bare ``0``) so the burn-in-cleanliness threshold lives in
#: one reviewable place rather than a magic literal inside the recommendation.
_MAX_BURN_IN_DIVERGENCES = 0


def cutover_burn_in_report(
    flags: Iterable[CutoverFlag],
    activation_counts: Mapping[str, int],
    today: date,
) -> list[dict[str, object]]:
    """Join the ``cutover``-stage manifest against activation counts into rows.

    The retirement-half mirror of :func:`audit_flags_report`: for each entry at
    ``stage == "cutover"`` — the only stage a collapse decision is still pending
    for (``retain`` is a permanent fallback, ``collapse_eligible`` / ``collapsed``
    are past the decision) — emit one row an operator can read to judge whether the
    flag has burned in enough to collapse. Non-``cutover`` stages are excluded.

    Each row carries ``flag``, ``cutover_cycle`` (the flip-on date), ``age_days``
    (*today* minus the flip-on date), ``review_by``, ``past_floor`` (``today >=
    review_by`` — the review horizon doubles as the burn-in floor), ``gate``,
    ``divergences`` (``activation_counts.get(gate, 0)`` when a divergence gate is
    wired, else ``None`` for unmeasured), ``recommend_collapse_eligible``, and the
    manifest ``note``. The recommendation is ``past_floor and divergences is not
    None and divergences <= _MAX_BURN_IN_DIVERGENCES``: a flag past its floor whose
    divergence gate has fired zero times is the clean burn-in a collapse needs, and
    a flag with no divergence gate (``divergences is None``) is never recommended,
    since burn-in cannot be confirmed without evidence. Purely advisory — like its
    experimental-flag sibling it only reports — and stays clock- and I/O-free
    because the CLI supplies both *activation_counts* and *today*.
    """
    rows: list[dict[str, object]] = []
    for f in flags:
        if f.stage != "cutover":
            continue
        divergences = (
            activation_counts.get(f.gate, 0) if f.gate is not None else None
        )
        past_floor = today >= parse_review_by(f.review_by)
        recommend = (
            past_floor
            and divergences is not None
            and divergences <= _MAX_BURN_IN_DIVERGENCES
        )
        rows.append(
            {
                "flag": f.flag,
                "cutover_cycle": f.cutover_cycle,
                "age_days": (today - parse_review_by(f.cutover_cycle)).days,
                "review_by": f.review_by,
                "past_floor": past_floor,
                "gate": f.gate,
                "divergences": divergences,
                "recommend_collapse_eligible": recommend,
                "note": f.note,
            },
        )
    return rows
