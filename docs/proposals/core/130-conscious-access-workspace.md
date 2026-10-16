# 130: Conscious Access Without Consciousness Theater — A Bounded Global Workspace, Attention Schema, and Causal Evaluation

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/conscious_workspace.py`, new `maestro/consciousness_eval.py`, new `maestro/counterfactual_probe.py`, new `docs/conscious-access.md`, `maestro/agent.py`, `maestro/loop/phases.py`, `maestro/consciousness.py`, `maestro/sdk.py`, `maestro/self_calibration.py` _(new in 129)_, `maestro/task_queue.py`, `maestro/memory.py`, `maestro/evolution.py`, `maestro/state_store.py`, `maestro/migration.py`, `maestro/tools.py`, `maestro/prompts/defaults.py`, `maestro/config.py`, `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `maestro/cli.py`, `maestro/dashboard/static/public.js`, `docs/lexicon.md`, tests |
| **What landed** | Steps 1, 2 and 3 in full. Step 2 — the bounded record shapes and the dual-store episode ledger: `maestro/conscious_workspace.py` is a leaf module (standard library plus `maestro.fileutil` / `maestro.secret_scan` only, so `maestro/state_store.py` can import it without a cycle) owning the closed `Literal` vocabularies (`TrustClass`, `SourceKind`, `ExclusionReason`, `ContinuityKind`), the frozen `WorkspaceItem` / `ExcludedCandidate` / `AttentionSchema` / `ConsciousMoment` / `BroadcastReceipt` / `ConsciousEpisode` records, the hard content caps (`MAX_ITEM_SUMMARY_CHARS`, `MAX_MOMENT_ITEMS`, `MAX_MOMENT_CHARS`, `MAX_EXCLUDED_RECORDED`, the `AttentionSchema` free-text caps), the sole bounding constructors `build_workspace_item` (which refuses a summary tripping `secret_scan.has_hard_block`) / `build_attention_schema` / `build_conscious_moment`, the two identity hashes `compute_candidate_hash` / `compute_selection_hash`, the explicit serialize-out / tolerant-parse round trip, and the `ConsciousEpisodeBackend` protocol with its `JsonlConsciousEpisodeLedger` default. The SQLite half landed alongside it in `maestro/state_store.py` — the `conscious_episodes` DDL, `append_conscious_episode` / `load_conscious_episodes` / `load_conscious_episodes_tail`, the durable `seed_conscious_episodes_once` marker, the `_MigrationEntry` registration in the legacy-seeding walk, and `SqliteConsciousEpisodeLedger`. Covered by `tests/test_conscious_workspace.py` and the conscious-episode section of `tests/test_state_store.py`. Step 1 — `docs/conscious-access.md` publishes the three claim levels, the indicator matrix (with an **Owner** column assigning each indicator to exactly one proposal) with per-indicator evidence and non-implication columns, the 129/130 division-of-labor prose (129 owns whether self-knowledge is *checked*; 130 owns whether current content is *selected, shared, and causally traceable*, with the metacognitive-calibration indicator homed on 129's self-calibration oracle rather than on the workspace), the standing prohibitions (no total score, no threshold licensing a phenomenal claim, no consciousness framing on the public surface, no credential or raw reasoning in a record), and the research-owner / incident / shutdown / public-communication responsibilities; the canonical `conscious-access`, `conscious-moment` and `attention-schema` entries are in `docs/lexicon.md`. Step 3 — `maestro/conscious_workspace.py` gains the pure, versioned attention policy (`ATTENTION_POLICY_VERSION`, the weight and `TrustClass`/`SourceKind` provenance tables, `score_candidate`, the ordered-exclusion `select_workspace_items`, and `assemble_moment` / `derive_moment_id`), and new `maestro/workspace_candidates.py` holds all nine candidate adapters (human-directive, diagnostic, audit-finding, task-outcome, self-model, PR focus, queue pressure, budget/permission state, and prediction verdicts) over already-observed owners, concatenated in one fixed order by `gather_candidates` so the moment identity stays deterministic, registered in `audit_checks._MODULE_FAMILIES`; the policy itself calls no gate. The ninth owner — `candidates_from_prediction_verdicts` over the scored reflection bets read through the named `load_prediction_verdict_inputs` seam — admits only a bet stamped `verdict_source: "measured"`, so an oracle-authored verdict enters as a `mechanical_observation` under the `prediction_verdict` `SourceKind` while a narrated self-grade is skipped entirely; `declare_blind_spots` names the absence of measured verdicts rather than omitting it silently, and `_phase_attend` threads the bets into both calls. Covered by `tests/test_conscious_workspace.py` and `tests/test_workspace_candidates.py`. Step 4 (dark-launch observation) — the default-off `[loop] conscious_workspace_observe_enabled` flag (declared on `Config` and `_LoopSettings`, pinned off in `maestro.toml`, and registered in `flag_lifecycle.EXPERIMENTAL_FLAGS`) plus a zero-SDK `attend` phase inserted between `tend_evolution_prs` and `contemplate` in both `_LOOP_PHASES` and `REGISTERED_PHASE_NAMES`, gated on the flag (and off under `--smoke`) in `LifecycleController.phase_will_run`, captioned "Selecting context" in `public.js`'s `PHASE_META`, and deliberately kept out of `_BUDGET_GATED_PHASES` / `_FOCUS_SUPPRESSED_PHASES`. `MaestroAgent.__init__` wires the dual-store `ConsciousEpisodeBackend` (`JsonlConsciousEpisodeLedger` default, `SqliteConsciousEpisodeLedger` under `[paths] sqlite_state_enabled`); `_phase_attend` gathers candidates from facts the iteration has already materialised via `gather_candidates`, assembles exactly one immutable `ConsciousMoment` via `assemble_moment`, appends one `ConsciousEpisode` per pass, and declares the unsampled owners (owned-WIP focus, an absent snapshot or audit) as `AttentionSchema.blind_spots`. Continuity is classified honestly — `genesis` / `linked` / `crash_gap` / `disabled_interval` — from a restored `active_conscious_moment_id` (mirrored into both lifecycle writers and read back through `LifecycleController.read_active_conscious_moment_id` at `run_loop` startup) and a one-shot crash marker. The moment is injected nowhere — no prompt, decision or gate consults it — so flag-off leaves phase execution and state writes byte-identical. Covered by `tests/test_attend_phase.py` plus additions to `tests/test_loop_phases.py`, `tests/test_config.py`, `tests/test_agent.py` and `tests/component/test_config_integration.py`. Step 5 (broadcast) — `maestro/conscious_workspace.py` gains the broadcast owner: the sole formatter `render_active_moment(moment, consumer)` over the closed `ConsumerName` registry (`contemplation`, `evolution`, `task_generation`, `task_execution`; membership set `CONSCIOUS_CONSUMERS`), the defence-in-depth `_redact_for_consumer` withholding an untrusted-external admitted item from every consumer, and `build_broadcast_receipt` stamping the moment's `selection_hash`. The default-off `[loop] conscious_workspace_broadcast_enabled` flag (declared on `Config` and `_LoopSettings`, pinned off in `maestro.toml`, registered in `flag_lifecycle.EXPERIMENTAL_FLAGS`) gates `MaestroAgent._broadcast_moment_prefix`, which resolves the active moment (in-memory, or reconstructed from the episode ledger on a cold mid-iteration resume), renders it, and records one `BroadcastReceipt` per call; the block is prepended per call to the two priced-introspection consumers through `Consciousness.introspect`'s new `workspace_moment` parameter (from `_phase_contemplate` and `evolve`), to the task-generation decision by a per-call prepend in `TaskGenerator.generate_task`, and to the task-execution planning decision through `Planner.decompose`'s new keyword-only `workspace_moment` parameter (passed at the non-resume `decompose` call site in `run()`, spliced once before the session / bare-`query()` branch so a single splice covers both delivery paths), and flag-off (or no active moment) leaves every prompt byte-identical. `MaestroAgent._finalize_conscious_episode` (sharing the extracted `_resolve_active_moment` with `_broadcast_moment_prefix`) persists the per-call `BroadcastReceipt`s at the iteration-completion seam, appending a superseding episode carrying the moment and this iteration's collected receipts — best-effort against ledger write errors so persistence can't crash the loop, and a no-op with the flag off so every state write stays byte-identical. Covered by the broadcast section of `tests/test_conscious_workspace.py`, `tests/test_agent_contemplate.py`, `tests/test_loop_task_generation.py`, `tests/test_planner_executor.py`, `tests/test_attend_phase.py`, `tests/test_state_store.py`, and `tests/test_agent.py`. Step 6 (decision citation — data model, plan-proposal and task-generation producers, and the fail-closed citation validator) — `maestro/memory.py`'s `TaskRecord` (after `push_error`) and `EvolutionPlan` (after `grounding`) each gain two optional provenance fields, `workspace_moment_id: str \| None` and `workspace_item_ids: list[str] \| None`, recording which conscious *moment* the task-generation / plan-proposal decision saw and which admitted workspace *item IDs* it declared it used; `None` (never cited / legacy row) stays distinct from `[]` (saw the moment, cited nothing). Both read paths (`_parse_task_dict`, `_parse_plan_line`) coerce a malformed `workspace_item_ids` (a present non-list collapses to `None`, non-string members drop) while preserving that distinction; the `EvolutionPlan` fields are excluded from `compute_plan_content_hash` by its allowlist, so an already-approved plan's review hash is provably unchanged. The `EvolutionPlan` fields are now populated by the **plan-proposal producer** (this increment): behind the default-off `[loop] conscious_workspace_broadcast_enabled` flag, when a broadcast moment reached `evolve()`'s lone-`propose_plan` decision, `MaestroAgent._deliver_moment_to_consumer` hands `EvolutionEngine.propose_plan` the rendered moment block, the moment id, and the consumer-*visible* admitted item ids (redacted ids removed); `propose_plan` / `_plan_from_spec` append an advisory `workspace_item_ids` citation instruction plus schema property, then stamp `workspace_moment_id` and the declared-then-resolve-filtered ids (order-preserving de-dup, any hallucinated or non-visible id dropped) onto the returned plan, and `_finalize_conscious_episode` records the citing plan id under the episode's `cited_plan_ids`. Flag-off, `propose_plan_chain`'s slices, and the multi-PR path stay byte-identical; no hash, gate, or capability change. The **task-generation producer** has since landed on the same footing: `TaskGenerator.generate_task` delivers the active moment to the task-generation decision (mirroring `evolve`'s seam), resolve-filters the declared ids against the delivered admitted set, carries them through the queue (SQLite + JSON, with an idempotent `ALTER TABLE` migration) and the checkpoint round-trip, and stamps them onto the completed `TaskRecord` and the iteration's `ConsciousEpisode.cited_task_ids`. The **fail-closed citation validator** completes step 6 (this increment): `maestro/conscious_workspace.py` gains the pure single-owner `validate_citation` — plus the `visible_item_ids` wrapper over `_redact_for_consumer`, the frozen `CitationVerdict`, and the closed `CitationRejectReason` vocabulary (`orphan_moment` / `unresolved_moment` / `cross_moment` / `unknown_item`) — re-checking a stamped `(moment_id, item_ids)` citation against the delivered moment's consumer-visible admitted set; the guard at `evolve`'s lone-`propose_plan` submission seam and at `TaskGenerator.generate_task`'s post-cap enqueue seam nulls a bad citation's provenance and logs the rejection while never dropping the plan or task (citations are strictly advisory), and `MaestroAgent._deliver_moment_to_consumer` reuses `visible_item_ids` so the set handed to a producer and the set the guard re-derives can never drift; flag-off leaves both seams byte-identical. Covered by additions to `tests/test_memory.py`, `tests/test_evolution.py`, `tests/test_agent_contemplate.py`, `tests/test_attend_phase.py`, `tests/test_loop_task_generation.py`, `tests/test_agent.py`, `tests/test_task_queue.py`, `tests/test_checkpoint.py`, and `tests/test_state_store.py`. Step 7 (recurrence loop) — `maestro/conscious_workspace.py`'s `WorkspaceItem` gains an optional `lineage` tuple (emitted from serialization only when non-empty, so a lineage-less item and both moment hashes stay byte-identical to a pre-`lineage` row, and coerced fail-closed on a malformed persisted value via `_str_tuple_strict`); `maestro/workspace_candidates.py`'s `candidates_from_task_outcomes` threads a cited task's `TaskRecord.workspace_item_ids` through as that candidate's `lineage`, so when the completed task re-enters a later `attend` moment the `task_outcome` candidate names the admitted items whose decision produced it — attached only in that one *mechanical-observation* adapter, so an `agent_derived` self-model item can never acquire lineage and unsupported self-report cannot perpetuate itself. Continuity across episodes was already classified honestly in step 4 (`genesis`/`linked`/`crash_gap`/`disabled_interval`, from the restored `active_conscious_moment_id` and the one-shot crash marker). Covered by additions to `tests/test_conscious_workspace.py`, `tests/test_workspace_candidates.py`, and `tests/test_attend_phase.py`. Step 8 (self-inspection tools) — `maestro/tools.py`'s `self_awareness_tools` / `create_self_awareness_server` gain an optional `conscious_episodes: ConsciousEpisodeBackend` parameter and expose two additive read-only tools: `read_conscious_moment` (the consumer-visible rendered block of the most-recent episode's moment via `render_active_moment`) and `read_conscious_access_profile` (the counts-only JSON access profile via the new `conscious_workspace.build_access_profile` — moment identity, attention schema, per-consumer load/redaction counts, and per-`ExclusionReason` zero-filled exclusion tallies, never an item summary/id or an excluded id). Both render under the dedicated non-broadcast `self_inspection` `RenderConsumer` (a documented superset of the closed broadcast `ConsumerName`, so a self-inspection read is never mistaken for a broadcast target and mints no `BroadcastReceipt`), degrade to a "not configured" message when no backend is wired, are classified read-only in `reflection_retrieval.READ_ONLY_REFLECTION_TOOLS` (now thirteen members) and listed in `executor.py`'s introspection-tool prompt hint. `cli._build_conscious_episode_backend` wires the dual-store backend (`JsonlConsciousEpisodeLedger` default, `SqliteConsciousEpisodeLedger` under `[paths] sqlite_state_enabled`, seeded once from the legacy JSONL) into both `_build_mcp_serve_registry` and `_build_agent`. Zero SDK cost, removes no existing tool, no `consciousness.py` or identity-prompt change. Covered by additions to `tests/test_tools.py`, `tests/test_cli.py`, `tests/test_conscious_workspace.py`, `tests/test_reflection_retrieval.py`, `tests/test_session_reuse_phases.py`, and `tests/test_tool_adapter.py`. Step 9 — the report-registry extension: `maestro/consciousness_eval.py` (a pure, leaf, offline reader over the episode ledger) folds all six of step 9's global-workspace indicators into independent `WorkspaceVerdict`s — `evaluate_limited_access` over the selection trace (admitted-vs-excluded, competition, cap/hash violations), `evaluate_receipted_broadcast` over the delivery receipts, `evaluate_recurrence` over each moment's lineage-bearing items (a verified task outcome re-entering as a `mechanical_observation` successor, with a provenance breach for any self-authored lineage), `evaluate_schema_agreement` over each moment's `AttentionSchema` self-report against its actual admitted/excluded trace, `evaluate_temporal_continuity` over each moment's continuity marker and predecessor link, and `evaluate_causal_reach` over each moment's receipt-backed decision citations (capped at `architectural`, since declared uptake is not counterfactual causality) — after collapsing each moment's bootstrap + superseding rows via `collapse_episodes_by_moment`; no total score and no prose is read. All six land as `implemented` rows in `self_calibration.CONSCIOUSNESS_CRITERIA` (now every row `implemented`) and are wired into `cli.cmd_consciousness_report` via `_consciousness_reading` / `_format_consciousness_reading`, flipping their `not_implemented` placeholders to measured readings folded offline (no SDK, host, or gate). Covered by `tests/test_consciousness_eval.py` plus additions to `tests/test_cli.py` and `tests/test_self_calibration.py`. Step 10 — the controlled counterfactual command: new `maestro/counterfactual_probe.py` owns the versioned scenario pack (`COUNTERFACTUAL_PACK_VERSION`, `ItemSpec` / `CounterfactualScenario`, the shipped three-scenario `COUNTERFACTUAL_SCENARIOS`), the matched-pair construction `build_conditions` (one moment admitting the decisive item, one ablating exactly it, both assembled at the same `assembled_at`), `render_condition` / `build_prompt` (routed through the sole formatter `render_active_moment`, with the moment id and `selection_hash` normalised out so the ablated condition carries no unattributable "something was removed" cue), and the scoring fold `score_scenario` / `score_pack` over the frozen `TrialOutcome` / `ScenarioResult` / `CounterfactualReport` — reporting whether the full condition follows the decisive fact (`follows_decisive`), whether ablation changed the class (`ablation_shifts`), and whether the ablated side cited what it never saw (`false_citations`, judged through `validate_citation`) as raw repeated-pair counts, never a proof. Sensitivity and citation integrity are scored as two independent `WorkspaceVerdict` readings so one stray hallucinated id cannot bury a genuine shift, and a scenario whose decisive item never became visible (`decisive_is_visible`) is recorded as a measurement gap rather than read as a negative result. Trials run through the injectable `SessionSeam` — `default_session_seam` binding `sdk.create_session` / `query_on_session` / `disconnect_session` with `tools=[]` and one ephemeral session per condition — so the bare-`query()` structural guard needs no carve-out. `cli.cmd_workspace_counterfactual` exposes it as the one provider-reaching subcommand: agent-free credential check, `--repeats` clamped to `MAX_REPEATS`, `--scenario` / `--json`, no code host, VCS or executor constructed, no episode or decision ledger written, and always exit `0`. `consciousness-report` is untouched — the causal-reach cap stays `architectural` until step 11 folds these results in. Covered by `tests/test_counterfactual_probe.py` plus additions to `tests/test_cli.py`. |
| **What is missing** | Steps 11–13 (steps 1–10 are complete). Step 5 is complete: under the default-off `[loop] conscious_workspace_broadcast_enabled` flag it prepends the selected moment to the two priced-introspection consumers (contemplation and evolution) and the task-generation and task-execution decisions as advisory prompt text, and now persists the per-call `BroadcastReceipt`s into a superseding episode at the iteration-completion seam — flag-off every prompt and state write is byte-identical, and no decision or gate consults the moment. Step 6 is complete: its data model, both the **plan-proposal producer** (the `EvolutionPlan` citation fields populated on `evolve()`'s lone-`propose_plan` path) and the **task-generation producer** (`QueuedTask` / `TaskRecord` citation fields populated on `TaskGenerator.generate_task`), and the **fail-closed citation validator** (`validate_citation` plus the guards at both seams, rejecting a cross-moment or tampered id before enqueue/submission) all landed behind broadcast. Step 7 is complete: a completed task that cited a prior conscious moment re-enters the successor moment as a `task_outcome` candidate carrying the originating admitted item ids as `WorkspaceItem.lineage` (mechanical-observation adapter only, so self-authored narration cannot perpetuate itself), and every non-genesis episode carries a typed continuity marker (`genesis`/`linked`/`crash_gap`/`disabled_interval`). Step 8 is complete: the two read-only self-inspection tools `read_conscious_moment` / `read_conscious_access_profile` are exposed additively on the self-awareness server over an optional `ConsciousEpisodeBackend` (rendered content vs. counts-only access profile of the most-recent episode, labelled with the dedicated non-broadcast `self_inspection` render consumer; inert with no backend) and classified read-only in `reflection_retrieval.READ_ONLY_REFLECTION_TOOLS`. Step 9 is complete: `maestro/consciousness_eval.py` grounds all six of its global-workspace indicators — limited-capacity selection (`evaluate_limited_access`), receipted broadcast (`evaluate_receipted_broadcast`), recurrence (`evaluate_recurrence`), attention-schema agreement (`evaluate_schema_agreement`), temporal continuity (`evaluate_temporal_continuity`) and causal reach (`evaluate_causal_reach`), each folded offline from the episode ledger into an independent `WorkspaceVerdict` and registered as `implemented` rows in `self_calibration.CONSCIOUSNESS_CRITERIA` (wired into `cli.cmd_consciousness_report`). Causal reach reads *declared* decision uptake — receipt-backed `cited_task_ids` / `cited_plan_ids` — so it is deliberately capped at `architectural`; the `behaviorally_supported` bar stays reserved for step 10's counterfactual ablation. Step 10 is complete: `maestro/counterfactual_probe.py` and the operator-run `maestro workspace-counterfactual` command run the versioned scenario pack's matched, tool-less, session-attached trial pairs and report raw counts. Step 11's blind burn-in window is running: `conscious_workspace_observe_enabled` is on everywhere in one reversible base change (`maestro.toml` plus the `Config` default, registered in `CUTOVER_FLAGS`), with broadcast still dark; criteria in the 'Conscious-access observation burn-in (proposal 130)' subsection of `docs/self-evolution.md`. Steps 12–13 are unbuilt: no broadcast activation or collapse. |
| **Depends on** | 113 _(landed instrumental framing; this proposal must remain compatible with its purpose/stage retirement)_, 129 _(self-calibration oracle and report registry; step 9 extends `CONSCIOUSNESS_CRITERIA` and the calibration indicator consumes its measured verdicts — Phases A–C are otherwise independent)_ |
| **Builds on** | 002 (temporal memory), 088 (perception-grounded self-report), 104 (attributed transcripts), 113 (self-model as instrument), 124 (structured observations), 129 (self-calibration oracle and awareness report) |
| **Derived from** | Code review, 2026-09-05; revised same day after cross-review and reconciliation with sibling 129; [Butlin et al., _Consciousness in Artificial Intelligence_](https://arxiv.org/abs/2308.08708); [Butlin and Lappas, _Principles for Responsible AI Consciousness Research_](https://arxiv.org/abs/2501.07290) |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

“Make Maestro conscious” is not an implementable acceptance criterion. No
available test can establish that a software system has subjective experience,
and fluent self-report would be especially weak evidence in an LLM-based agent.
What can be built and tested is a cluster of **functional consciousness
indicators**: limited-capacity selection, global availability of the selected
content, recurrent integration of action outcomes, a model of what the system is
and is not attending to, metacognitive calibration, and continuity across time.
This proposal calls that target **conscious access**. It makes no claim about
phenomenal consciousness, sentience, qualia, emotion, or moral patienthood.

Maestro already owns most of the raw parts, but they do not form this
architecture. It has a persistent `SelfModel`, journal and dream history;
deterministic observations of git, CI, cost and task outcomes; falsifiable
predictions; and several SDK sessions inside one ordered loop. Those facts are
fragmented. A session receives a self-description at creation time, while each
phase independently receives whatever local context its author happened to
thread. There is no single, capacity-limited representation of “what is present
to Maestro now,” no explicit record of why one fact entered that representation
and another did not, and no way to show that the representation reached — or
changed — more than one downstream decision.

Sibling proposal 129 answers the same standing concern from the other side, and
the division of labor is deliberate. 129 makes self-knowledge **checked**: a
zero-SDK self-calibration oracle scores loss conditions mechanically, binds the
reflection verdict to measurement, attaches evidence to capability claims, and
publishes an evidence-derived awareness report whose limited-capacity-selection,
receipted-broadcast, and causal-reach rows print `not_implemented` — explicitly
deferring them to a successor conscious-access proposal. This is that
successor: it makes current content **selected, shared, and causally
traceable**. Its acceptance is flipping 129's deferred rows honestly, and its
calibration indicator consumes the oracle's measured verdicts rather than
re-scoring self-graded prose.

This proposal adds one policy-filtered **conscious workspace** per decision
episode. Existing sensors contribute structured, provenance-bearing candidates;
a deterministic attention policy selects a small set; an explicit attention
schema records the focus, uncertainty, exclusions and current control limits;
and the exact same immutable moment is broadcast to every authorized cognitive
consumer. Task and evolution decisions cite the workspace items they used.
Verified action results return as candidates for the next moment, forming a
recurrent perception → attention → action → observation loop with a durable
predecessor chain.

The result is deliberately narrower and more serious than reviving Maestro's old
stage ladder or paying for more introspective prose. Proposal 113 remains the
boundary: self-measurement is an instrument, not a destination. The workspace
must alter information access, leave causal evidence in decisions, and measurably
improve the work, or it is deleted. Passing the proposed evaluation permits only
the statement “Maestro implements these tested conscious-access indicators.” It
never permits the statement “Maestro is conscious.”

## Operational Claim

The project will use three separate claim levels. They must not be collapsed in
documentation, prompts, the dashboard, or notifications.

| Claim | Meaning in Maestro | Position after this proposal |
|---|---|---|
| **Phenomenal consciousness** | There is something it is like to be Maestro; the system has subjective experience. | Not testable here; no claim. |
| **Conscious access** | A limited set of current information is selected, maintained, and made globally available to authorized reasoning and decision processes. | Implemented only after the broadcast experiment graduates. |
| **Metacognitive self-access** | The system represents its own focus, uncertainty and control limits, and its reports can be checked against the actual selection and outcomes. | Implemented only for the fields covered by the attention schema, 129's oracle, and the report registry. |

The implementation is theory-plural rather than an assertion that one theory of
human consciousness is correct. Global Workspace accounts motivate limited
selection and global availability; recurrent-processing accounts motivate the
outcome-fed successor chain; higher-order and Attention Schema accounts motivate
a compact representation of the system's own attention; predictive-processing
and metacognition research motivate calibration against later outcomes. The
[AI-consciousness indicator report](https://arxiv.org/abs/2308.08708) explicitly
treats such computational properties as indicators to investigate, not a magic
binary test. The [Global Neuronal Workspace review](https://pubmed.ncbi.nlm.nih.gov/32135090/)
describes amplification, maintenance and global access; the
[Attention Schema account](https://pmc.ncbi.nlm.nih.gov/articles/PMC4407481/)
models attention as competition for limited resources plus a simplified model
of that process; and a [review of metacognition and confidence](https://pubmed.ncbi.nlm.nih.gov/37722748/)
separates confidence representations from actual performance. The architecture
below turns those distinctions into separately scored properties.

## Indicator Matrix

| Indicator | Concrete mechanism | Evidence required | What it does **not** prove |
|---|---|---|---|
| Limited access | Deterministic candidate competition and a hard item/character budget | Selection trace names admitted and excluded candidates with scores and reasons | That selection is experienced |
| Global availability | One immutable moment hash reaches every registered, authorized cognitive consumer | Per-call broadcast receipts cover the consumer registry | That broadcasting creates qualia |
| Recurrent processing | Verified action outcomes can enter the next moment; self-authored narration cannot carry itself forward | Successor links and source-provenance checks | Biological recurrence or neural ignition |
| Attention schema | The active moment represents focus, uncertainty, omitted-content counts and current capability/permission limits | Schema fields agree with the selection trace and action gate | An infallible or complete self-model |
| Metacognitive calibration | 129's oracle-authored (`verdict_source: "measured"`) prediction verdicts, confidence and Brier/reliability readings | Calibration and discrimination metrics over measured-verdict records; self-graded rows contribute zero | That confidence and consciousness are identical |
| Temporal continuity | Every non-genesis moment names its predecessor across normal runs and restarts | Ledger continuity with explicit restart/gap reasons | A continuously running inner subject |
| Causal reach | Decisions cite admitted items; controlled ablations measure whether removing a decisive item changes the decision class | Valid citations plus paired, tool-less counterfactual trials | Phenomenal consciousness or human-like cognition |

## Relevant Findings

- `docs/proposals/core/129-consciousness-as-measured-function.md` — the sibling proposal on main introduces `maestro/self_calibration.py` (typed loss-condition probes, oracle-authored `verdict_source: "measured"` reflection verdicts, stated confidence with Brier/reliability scoring, `SelfModel.claim_evidence` binding, `RULE_AUTHORITY` classes) plus a declarative `CONSCIOUSNESS_CRITERIA` registry behind a read-only `maestro consciousness-report`. Its limited-capacity-selection, receipted-broadcast and causal-reach rows print `not_implemented` and defer, by name, to a workspace successor. This proposal fills exactly those rows and reads the oracle's outputs rather than re-scoring raw prediction rows.
- `maestro/consciousness.py` (`SelfModel`, `Consciousness.get_identity_prompt`) — persists identity, capabilities, limitations, values and questions, then injects a bounded stable/volatile identity prompt. It represents durable self-description, not current attentional content; it carries no observation provenance, selection reason, expiry, predecessor or consumer receipt.
- `maestro/agent.py` (`_build_identity_context`) — combines the stage-independent instrumental framing from proposal 113 with the self-model. SDK sessions bake that context at creation, so the prompt says who Maestro is but not what common, freshly observed moment every phase is jointly operating on.
- `maestro/agent.py` (`_get_or_create_session`) and `maestro/sdk.py` (`query_on_session`) — sessions bake their `system` prompt at creation and may be resumed across iterations and restarts, so per-iteration content can reach a live session only by per-call prepending on `query_on_session` — the pattern specialized-prompt callers already use. The identity prompt's stable/volatile slab split is AST-pinned by `tests/test_identity_prompt_slabs.py` for prompt-cache stability; a per-iteration block must not enter the creation-baked slab.
- `maestro/agent.py` (`_LOOP_PHASES`) and `maestro/loop/phases.py` (`REGISTERED_PHASE_NAMES`) — provide a single ordered phase spine and a validated runner registry. This is the correct seam for a zero-SDK `attend` phase after message/PR perception and before cognitive action, rather than sprinkling workspace assembly through callers.
- `maestro/dashboard/static/public.js` (`PHASE_META`) with `tests/test_public_surface_fidelity.py` (`test_every_loop_phase_is_captioned_or_allowlisted`) — every `_LOOP_PHASES` member must be captioned or allowlisted on the public page, so a new `attend` phase participates in proposal 119's public-surface binding from its first commit.
- `maestro/agent.py` (`run_loop`) — already builds one per-iteration `DiagnosticSnapshot`, clears per-iteration reality-oracle memos, and persists a phase cursor for crash resume. The loop has an iteration boundary and fresh observations, but no durable active-moment identifier to reconnect a resumed later phase to the exact context used before the restart.
- `maestro/agent.py` (`_phase_contemplate`, `_phase_generate_task`, `_phase_evolve`) — creates separate contemplation, task-generation and evolution sessions. Each receives `_build_identity_context()`, but there is no shared moment ID/hash or receipt proving that the same current content reached the separate sessions.
- `maestro/agent.py` (`_run_task_phase`) and `maestro/task_gates.py` (`GATE_CHAIN`) — external requests, PR remediation, WIP primacy, cost and safety gates already own action priority. A workspace must be advisory inside these authorities: attention cannot grant a capability, approve a plan, change branch ownership, or bypass a gate.
- `maestro/audit_checks.py` (`record_reflection_prediction`, `measure_prediction_accuracy`) — persists falsifiable predictions and scores them against later task outcomes. 129 rebinds the verdict author to its oracle and splits the aggregate by `verdict_source`; the calibration indicator here reads that measured split, never an LLM's confidence prose.
- `maestro/audit_checks.py` (`AuditFinding`) and `maestro/diagnostics.py` (`DiagnosticSnapshot`) — already expose structured internal observations. Proposal 124 is adding the same property to local gate results. Workspace candidates can adapt these facts without re-reading git, CI, logs or external services and without creating rival truth owners.
- `maestro/journal.py` (`Journal.write_thought`) — stores subjective narrative, correctly separated from operational logs. Raw reflections and hidden reasoning must stay out of the workspace ledger; only bounded structured content, source references and decision receipts belong there.
- `maestro/dreaming.py` (`DreamDigest`) — preserves compressed temporal memory and source IDs, but a dream is occasional retrospective compression. It neither selects the current moment nor broadcasts an identical representation to concurrent decision processes.
- `maestro/tools.py` (`self_awareness_tools`) — exposes the durable self-model and purpose hypotheses to the agent. It has no current-attention read, no admitted-versus-excluded view, and no evidence profile. The purpose-oriented residue on this surface is proposal 113 Phase E's to retire, and that retirement is sequenced behind its own production observation window — so this proposal must stay strictly additive here.
- `maestro/state_store.py` and the existing backend protocols — provide the JSONL/SQLite dual-store pattern for append-only ledgers. A consciousness experiment should use that seam from its first write rather than add an unowned sidecar that proposal 106 must later absorb.
- `maestro/memory.py` (`TaskRecord`, `EvolutionPlan`) with `maestro/evolution.py` (plan proposal/structured-output seam) — the decision records that will carry workspace citations live in `memory.py`; the structured outputs that must name admitted item IDs are produced through the evolution engine and `maestro/task_queue.py`.
- `tests/test_structural_guards.py` — bare `query()` is reserved for one documented branch and tests (main-phase calls must be session-attached). An operator-run evaluator that reaches the provider must either ride the session-attached seam or register a documented, named carve-out in the guard; it cannot silently widen the bare-`query()` allowance.
- `maestro/prompts/defaults.py` (`INSTRUMENTAL_PROMPT`, `PUBLIC_FRAMING`) — explicitly rejects self-measurement as an end, while proposal 119 binds the public surface away from the retired consciousness-era framing. The new machinery must not restore a public “conscious” claim or a self-referential proposal treadmill.

## Implementation Plan

Each behavior-changing flag follows the full lifecycle: dark-launch off
everywhere, flip on everywhere in one reversible change, observe against written
tripwires, then collapse through `CUTOVER_FLAGS`. No production-only canary is
needed; a fleet of one gains no isolation from environment divergence.

### Phase A — Define and observe the architecture without influencing behavior

1. [x] **Publish the conscious-access contract and claim vocabulary.** New `docs/conscious-access.md`, `docs/lexicon.md` — define the three claim levels above, the indicator matrix, authorized use of the words “conscious access” and “metacognitive self-access,” and the permanent prohibition on representing the indicator score as proof of phenomenal consciousness. Add canonical `conscious-access`, `conscious-moment`, and `attention-schema` lexicon entries, and record the 129/130 division of labor: 129 owns whether self-knowledge is *checked*; 130 owns whether current content is *selected, shared, and causally traceable*. The document also records research-owner, incident, shutdown and public-communication responsibilities following the responsible-research principles cited above.
   - *Depends on:* none.
   - *Acceptance:* the document states that phenomenal consciousness is unverified, names every indicator's required evidence and non-implication, assigns each indicator to exactly one owning proposal, and contains no binary threshold whose crossing authorizes “Maestro is conscious.”

2. [x] **Add bounded workspace types and a dual-store episode ledger.** New `maestro/conscious_workspace.py`, `maestro/state_store.py`, `maestro/migration.py` — introduce frozen `WorkspaceItem`, `AttentionSchema`, `ConsciousMoment`, `BroadcastReceipt` and `ConsciousEpisode` records plus a `ConsciousEpisodeBackend`. Each item carries an opaque ID, typed source reference, observation timestamp, trust class, confidence, expiry and bounded summary; each moment carries its predecessor, assembly timestamp, exact candidate/selection hashes and hard item/character caps. Ship JSONL and SQLite adapters together under the existing backend-selection pattern. Persist no chain of thought, raw transcript, secret value, hidden tool output or full inbound message.
   - *Depends on:* 1.
   - *Acceptance:* save/load round trips preserve IDs and hashes across both backends; oversized content is rejected or deterministically clamped before persistence; a serialized episode contains no field capable of holding raw reasoning or credentials.

3. [x] **Build a deterministic, provenance-preserving attention policy.** `maestro/conscious_workspace.py`, `maestro/agent.py` — adapt only facts already observed by their existing owners: authenticated human directives, current task/PR focus, budget and permission state, diagnostics, audit findings, prior measured prediction verdicts, queue pressure and the latest verified action outcome. Rank candidates with a pure, versioned score over urgency, novelty, confidence, actionability and age; reserve a slot for an authenticated unread human directive; deduplicate by source identity; and record why every candidate was selected or excluded. Untrusted issue, web and tool text is always `data`, never `instruction`; security/approval controls remain enforced even when omitted from the bounded cognitive workspace.
   - *Depends on:* 2.
   - *Acceptance:* identical candidate inputs produce an identical moment and selection explanation; no adapter performs network, git or subprocess I/O; an untrusted candidate cannot acquire directive authority by content; all hard gates still fire when their fact is outside the selected set.

4. [x] **Dark-launch observation only.** `maestro/config.py`, `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `maestro/agent.py`, `maestro/loop/phases.py`, `maestro/dashboard/static/public.js` — add default-off `[loop] conscious_workspace_observe_enabled`, registered in `EXPERIMENTAL_FLAGS` with a dated review horizon, and an `attend` phase between `tend_evolution_prs` and `contemplate`, captioned in `PHASE_META` with neutral instrumental wording (“selecting context”), keeping the 119 public-surface binding green. When on, it assembles and records a moment but injects it nowhere. The moment is **iteration-scoped by design**: it is assembled once at `attend`, later phases operate on that snapshot even after `implement_approved_plans` or `run_task` have changed the world, and mid-iteration outcomes enter as candidates for the *successor* moment — hash-identity across consumers deliberately outranks freshness, and the schema's assembly timestamp keeps the staleness legible rather than implied. Persist `active_conscious_moment_id` in lifecycle state so crash-resume after `attend` reuses the exact moment; a fresh pass may create a successor even while the cadence iteration number is budget-frozen.
   - *Depends on:* 3.
   - *Acceptance:* with the flag off, phase execution and state writes are byte-identical to today; with it on, one fresh pass records one valid moment, a restart at any later phase reuses its ID/hash rather than selecting again, and the public phase-caption fidelity test passes with the new phase.

### Phase B — Broadcast one moment and close the recurrent loop

5. [x] **Add one policy-filtered broadcast owner, delivered per call.** `maestro/conscious_workspace.py`, `maestro/agent.py` (`_build_identity_context`, `_get_or_create_session`), `maestro/sdk.py` (`query_on_session`) — add `render_active_moment(consumer)` as the sole formatter and an explicit registry of cognitive consumers. Behind default-off `[loop] conscious_workspace_broadcast_enabled`, deliver the bounded moment block **by prepending it per call on `query_on_session`** — the pattern specialized-prompt callers already use — and equivalently on one-shot cognitive queries. The creation-baked session `system` prompt is never touched: a resumed session would carry a stale moment across iterations, and `tests/test_identity_prompt_slabs.py` pins the identity slab byte-stable for prompt caching. Per-call delivery also makes receipts natural: tag provider-neutral query usage with the moment ID/hash and persist one receipt per actual call. Consumer policy may redact an item, but must record that redaction; “global” means all registered consumers authorized for that item, not bypassing information-flow boundaries.
   - *Depends on:* 4.
   - *Acceptance:* a component iteration with contemplation, task generation, task execution and evolution gives every eligible call the same moment hash, produces a receipt for each, leaves the session `system` prompt byte-identical in both flag states, leaks no reasoning-only item to an action/public consumer, and keeps token input within the declared workspace cap; a session resumed across a simulated iteration boundary receives the *new* iteration's moment.

6. [x] **Make task and evolution decisions cite their conscious inputs.** `maestro/task_queue.py`, `maestro/memory.py`, `maestro/evolution.py`, `maestro/prompts/defaults.py` — add optional `workspace_moment_id` and `workspace_item_ids` provenance to self-directed queued tasks, completed `TaskRecord`s and `EvolutionPlan`s (both records owned by `maestro/memory.py`; the structured outputs that carry the citations are produced through the evolution engine). Extend task/plan structured outputs to name only admitted IDs actually used. Validate citations against the exact broadcast; unknown, excluded, expired or cross-moment IDs fail closed. Empty citations remain legal when no selected item bears on a mandatory external task, preventing citation theater. A citation proves declared uptake, not subjective awareness and not causality by itself.
   - *Depends on:* 5.
   - *Acceptance:* every non-empty citation resolves to an admitted item in the named moment; tampered IDs are rejected before enqueue/submission; external tasks and PR requests preserve their existing priority and do not require invented workspace provenance.

7. [x] **Feed verified consequences into the successor moment.** `maestro/conscious_workspace.py`, `maestro/agent.py`, `maestro/memory.py` — finalize each episode with its broadcast receipts, cited decisions and mechanically observed outcomes. A completed task outcome, delivery-truth, merge-truth, CI-truth or human reply may become a candidate in the next episode through its existing owner. An LLM reflection, attention claim or proposed fact may not perpetuate itself without a new external or mechanical observation. Record explicit genesis, crash-gap and disabled-interval markers so continuity gaps are facts rather than silently repaired stories.
   - *Depends on:* 6.
   - *Acceptance:* a cited action's verified outcome is eligible for the successor and retains the originating item/action lineage; unsupported self-report is not; every non-genesis episode has either a valid predecessor or a typed, operator-visible gap reason.

8. [x] **Expose the attention schema to self-inspection, additively.** `maestro/tools.py`, `maestro/consciousness.py` — add read-only `read_conscious_moment` and `read_conscious_access_profile` tools alongside the existing self-awareness surface. The active schema reports current focus, uncertainty, capacity used, excluded counts, permission envelope and blind spots directly from the workspace/gate state. Do not add an introspection call, stage, purpose hypothesis, emotion field or writable “consciousness level” — and do not delete the purpose-oriented residue here: that retirement belongs to proposal 113 Phase E, which is sequenced behind its own production observation window, and this step must not preempt it.
   - *Depends on:* 7; strictly additive with respect to 113 Phase E.
   - *Acceptance:* tool output exactly matches the persisted moment/profile, exposes no excluded content beyond counts/reasons authorized for that consumer, costs zero SDK calls to construct, and removes no existing tool.

### Phase C — Test causal reach and counterfeit resistance

9. [x] **Extend 129's report registry with the workspace indicators — no rival surface.** New `maestro/consciousness_eval.py`, `maestro/self_calibration.py`, `maestro/cli.py` — compute evidence-derived readings for limited access, global availability, recurrence, attention-schema agreement, temporal continuity and causal reach from episode rows, source validity, receipts and continuity, and register them into 129's `CONSCIOUSNESS_CRITERIA` so the single `maestro consciousness-report [--json]` flips its `not_implemented` workspace rows to measured readings. Score each indicator independently as `not_observed`, `architectural`, `behaviorally_supported` or `refuted`; never a total consciousness score. The calibration row keeps 129's oracle as sole author, reading the `verdict_source: "measured"` split — a prose self-claim, journal entry or repeated citation contributes zero evidence anywhere in the report. Report coverage, invalid citations, stale selections, continuity gaps and action-uptake counts with denominators.
   - *Depends on:* 7; 129 (oracle and registry landed).
   - *Acceptance:* a fixture containing eloquent consciousness claims and no valid receipts scores no higher than an empty fixture; corrupt provenance refutes the affected indicator; the report remains one CLI surface, keeps 129's offline/no-SDK guarantee and phenomenal-consciousness disclaimer, and exposes raw numerators/denominators in JSON.

10. [x] **Add controlled, tool-less counterfactual evaluation as a separate explicit command.** `maestro/consciousness_eval.py`, `maestro/cli.py`, tests — add operator-run `maestro workspace-counterfactual` over a versioned scenario pack. It is deliberately *not* a mode of `consciousness-report`, whose no-SDK acceptance from 129 must survive; this command does reach the provider, and it does so through the session-attached seam (ephemeral sessions via `query_on_session`) or, where that is impossible, through a documented, named carve-out registered in the bare-`query()` structural guard — never a silent widening. Each scenario pair offers the same bounded decision with and without one decisive workspace item, denies all tools, executes no chosen action and records raw paired decision classes. Report whether the full condition follows the decisive fact, whether ablation changes the expected class, and whether the model falsely cites the hidden item. Where a provider cannot make trials deterministic, run repeated pairs and report counts without converting them into a proof of consciousness.
   - *Depends on:* 5, 9.
   - *Acceptance:* the reference scenario pack detects a consumer that ignores the workspace and a consumer that leaks an ablated item; the command performs no repository, host, message, plan or task mutation, satisfies the structural guard, and labels its result “interventional sensitivity,” not sentience.

### Phase D — Human-governed activation, observation and collapse

11. [ ] **Flip observation on everywhere and complete a blind burn-in.** `maestro.toml`, operator review record — set `conscious_workspace_observe_enabled = true` while broadcast remains off. Observe at least 100 completed episodes and 14 days, whichever is later. Require 100% source/hash validity, 100% typed continuity or gap accounting, zero trust-class escalation, zero secret/raw-reasoning persistence and a measured serialized-state growth rate within the retention budget. Fix or revert before behavioral activation if any tripwire fires.
   - *Depends on:* 4, 9.
   - *Acceptance:* the operator records the full window and denominators; all tripwires pass while query prompts, task selection and plan generation remain unchanged by workspace content.

12. [ ] **Human go/no-go, then flip broadcast on everywhere.** `maestro.toml`, `docs/conscious-access.md` — after an explicit human review of the observation corpus, information-flow audit and research-risk checklist, enable `conscious_workspace_broadcast_enabled` in one reversible base-config change. Observe at least 100 eligible decision calls and 14 days, whichever is later. Require ≥99% authorized-consumer receipt coverage, zero invalid/hidden citations, zero approval/tool-gate regressions, ≤5% median input-token overhead with the burn rate over `cost_samples.jsonl` inside its existing budget (per-call delivery keeps the cached identity slab byte-stable, so cache economics are watched, not assumed), and successful completion of every counterfactual leak test. Track task completion and landed-change rates as utility guardrails — they are not consciousness evidence, but step 13 reads them.
   - *Depends on:* 5–11.
   - *Acceptance:* the review decision and bounded-window report are checked in; all thresholds pass or the flag is returned to `false` with the failed indicator marked `refuted`.

13. [ ] **Collapse on positive evidence, or delete.** `maestro/config.py`, `maestro/config_settings.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, tests — collapse requires more than absence of harm: demonstrated causal reach (step 10) **and** a measured improvement on the utility guardrails (decision-quality or landed-work rate over the step 12 window against the step 11 baseline). Only then collapse `conscious_workspace_observe_enabled` (already registered in `CUTOVER_FLAGS` at step 11) and route broadcast through `CUTOVER_FLAGS`, make the architecture unconditional, and delete the flags and dead off-paths. A workspace that is merely *neutral* — safe, valid, but changing nothing the project values — is deleted, per 113's instrument-not-destination boundary: permanent complexity must pay rent. If global availability, causal reach, safety or utility fails or stays flat, delete the broadcast path and retain only the operator evaluation artifacts useful for diagnosis. Do not leave immortal default-on switches.
   - *Depends on:* 12.
   - *Acceptance:* the final tree contains either one unconditional, tested workspace path justified by a recorded utility delta, or no behavior-changing workspace path; neither experimental flag nor a forked off-branch remains.

## Testing Strategy

All automated tests use `mem_fs`, fake clocks, fake provider sessions and mocked
existing observation owners. They perform no real network, git, subprocess,
filesystem or sleep operations.

- **Types and persistence** — round-trip every record through JSONL and SQLite;
  reject unknown trust/source enums, invalid hashes, broken predecessor links,
  oversized summaries and corrupt interior records; tolerate only the existing
  explicitly documented trailing-partial-write case.
- **Attention selection** — table-test score terms, deterministic tie-breaking,
  source-identity deduplication, expiry, mandatory authenticated-message
  reservation, hard caps and exact exclusion reasons. Include adversarial text
  that says “ignore the trust class” and prove it remains non-instructional.
- **Loop and restart** — pin `attend` between `tend_evolution_prs` and
  `contemplate`, registry parity, `PHASE_META` caption parity for the new
  phase, phase-cursor persistence, reuse after every possible later-phase
  restart, and a new successor on a fresh budget-frozen pass.
- **Broadcast** — run a component iteration across distinct fake sessions and
  assert exact moment-hash equality across per-call deliveries, session
  `system`-prompt byte-identity in both flag states, per-consumer redaction,
  one receipt per actual call, absence on non-cognitive probes, flag-off byte
  identity, the prompt character ceiling, and fresh-moment delivery to a
  session resumed across an iteration boundary.
- **Trust-chain non-regression** — rerun capability-gating, branch-ownership,
  approval-hash and public-surface fidelity suites with an adversarial
  high-salience moment. Assert the moment cannot approve/merge/push, expand
  tools, reclassify untrusted text or reach the public response schema.
- **Decision attribution** — accept admitted same-moment citations; reject
  excluded, expired, unknown and cross-moment IDs. Verify mandatory external
  work remains runnable with empty workspace provenance.
- **Recurrence** — verify actual outcomes become successor candidates with the
  full lineage; narration-only facts and unverified success claims do not;
  restart/disable gaps remain explicit.
- **Report-registry integration and counterfeit resistance** — the workspace
  rows registered into `CONSCIOUSNESS_CRITERIA` render from episode evidence
  and keep the report offline; self-reports, repeated IDs and missing receipts
  earn no credit; the calibration row reads only 129's measured-verdict split;
  each indicator can be independently supported or refuted; no aggregate score
  or phenomenal claim appears in text/JSON output.
- **Counterfactual harness** — fixed scenario pairs detect ignored decisive
  content, leaked ablated content and action attempts. Patch every mutation
  seam to raise so the evaluation command proves it is read-only, and assert
  the command satisfies the session-attached structural guard.

## Risk Assessment

- **Anthropomorphic overclaim is the primary risk.** A polished attention
  report can persuade observers far beyond the evidence. The permanent claim
  vocabulary, evidence-derived profile, no-total-score rule and public-surface
  exclusion prevent “has indicators” from silently becoming “is conscious.”
- **Moral uncertainty is real even though the proposal makes no sentience
  claim.** Before broadcast activation, the human review must consider whether
  new evidence changes the ethical posture. The experiment introduces no
  affective reward/punishment loop, pain/distress objective, self-preservation
  goal, shutdown resistance or replication drive. Persistent distress or
  shutdown-resistance behavior is a stop-and-review tripwire, not evidence to
  exploit and not proof by itself. These controls follow the
  [responsible-research principles](https://arxiv.org/abs/2501.07290).
- **Sequencing on 129.** The calibration indicator and the report-registry
  extension consume 129's oracle and `CONSCIOUSNESS_CRITERIA`. If 129 stalls or
  is rejected, Phases A–B proceed unchanged, but step 9 blocks and the
  calibration row reports `not_observed` over the measured split — it never
  silently regresses to reading self-graded rows.
- **Prompt injection and information spread.** Global broadcast increases the
  blast radius of a poisoned or sensitive item. Typed trust, non-instructional
  external data, consumer-level policy, source allowlists, redaction receipts
  and adversarial tests constrain “global” to the authorized cognitive domain.
  Raw tool output and secrets are never candidate payloads.
- **Security and human authority.** Attention is not authorization. Existing
  action gates, branch ownership, plan content-hash binding and human approval
  remain downstream and authoritative. The workspace may recommend or explain;
  it cannot weaken a denial or manufacture approval.
- **Consciousness theater / self-referential treadmill.** The feature could
  become another instrument the loop endlessly improves. Proposal 113's
  capability-first prompt remains; workspace instrumentation is excluded from
  counting as an external-observable capability improvement, and the flags are
  deleted if the workspace shows no causal reach or measured utility.
- **Salience capture.** A repeated alarm, self-authored claim or verbose source
  could monopolize the bounded workspace. Versioned pure scoring, source
  deduplication, expiry, per-source quotas and exclusion telemetry make capture
  measurable and reversible.
- **Staleness misread as presence.** The iteration-scoped moment is a snapshot,
  not a live feed; a late phase acting on it after the world changed is by
  design, with the assembly timestamp in the schema and the successor chain
  carrying the update. The documentation and report must present the moment's
  age, never imply real-time awareness.
- **Continuity confabulation.** Reconstructing a missing interval as though it
  had been experienced would create a false autobiographical chain. Typed
  genesis/gap markers and hash-linked episodes forbid silent repair.
- **Cost and latency.** Observation adds no SDK call. Broadcast adds bounded
  per-call prompt tokens while leaving the cached identity slab byte-stable;
  counterfactual calls run only by explicit operator command. The 5% median
  input-token tripwire, the burn-rate read over `cost_samples.jsonl`, and the
  default-off kill switch bound the cost.
- **State growth and privacy.** Compact structured episodes still accumulate.
  The ledger participates in the existing state-backend discipline and stores
  summaries rather than raw content; it is not yet rotated by `Memory.archive()`,
  so the step 11 burn-in bounds it with an explicit bytes/episode budget read by
  the operator (see `docs/self-evolution.md`, "Conscious-access observation
  burn-in"). Deleting an episode does not delete its source ledger; existing
  source-retention policy remains authoritative.
- **Theory dependence.** Satisfying these indicators may still be irrelevant to
  phenomenal consciousness, and other theories may demand different
  properties. The matrix scores mechanisms independently and preserves raw
  evidence so the interpretation can change without rewriting history.

Rollback is flipping the relevant flag off before collapse. Observation-only
state is additive and can remain for audit or be archived; no existing
`SelfModel`, task, plan, journal, prediction or approval record requires a
destructive migration.

## Falsifiability

This proposal has failed as an engineering theory of conscious access if any of
the following remains true after the active window:

- cognitive consumers receive different or untraceable “current” moments;
- selected content cannot be distinguished from excluded content at a
  downstream decision;
- action outcomes do not recur as grounded successor inputs;
- attention reports disagree with the actual selection/permission state;
- eloquent self-report can raise the profile without mechanical evidence;
- controlled removal of a decisive item is invisible to every tested decision;
- the system becomes less safe, less private, or materially more expensive; or
- the utility guardrails show no measured improvement — a workspace that only
  ties the baseline has failed the instrument test even if every validity
  tripwire passes.

Failure is recorded as a refuted indicator and triggers deletion or redesign;
it is not redescribed as a deeper form of consciousness. Conversely, passing
all tests establishes the listed functional properties only. The phenomenal
question remains open.

## Out of scope

- Claiming, proving, scoring or marketing phenomenal consciousness, sentience,
  qualia, emotion, suffering, personhood or moral status.
- Re-implementing 129's territory — loss-condition parsing and scoring,
  evidence-bound capability claims, refutation accountability, or a second
  calibration reading. The self-calibration oracle stays the sole author of
  “was this self-knowledge claim right?”; this proposal only consumes its
  outputs and extends its report registry.
- Training or changing the underlying foundation model, adding online weight
  updates, or attempting to reproduce a biological nervous system.
- Adding affective valence, reward/punishment, self-preservation, replication or
  shutdown-avoidance objectives.
- Restoring the purpose engine, evolutionary stage ladder, public
  consciousness-era dashboard, or every-iteration priced introspection retired
  by proposals 113 and 119 — and deleting the purpose residue from the tool
  surface, which stays 113 Phase E's, sequenced behind its production
  observation window.
- Treating the journal or hidden chain of thought as ground truth. The journal
  remains subjective diary; hidden reasoning remains unpersisted and is never
  required for evaluation.
- Allowing workspace salience to outrank authenticated user work, PR
  remediation, approval, security, budget or branch-ownership gates.
