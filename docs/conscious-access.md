# Conscious access without consciousness theater

This document is the standing contract for Maestro's conscious-access work
(proposal 130). It fixes the vocabulary that everything built on
`maestro/conscious_workspace.py` will be measured against, so a fluent attention
report can never quietly drift into a claim it has not earned. It is
prescriptive: the record shapes, evaluations, and public surface are bound by
what is written here, not the other way round.

The permanent boundary this work operates under: **the workspace is an
instrument.** It must eventually alter information access, leave causal evidence
in decisions, and measurably improve the work, or it is deleted. Passing any
evaluation built on it licenses exactly one sentence — *"Maestro implements these
tested conscious-access indicators"* — and never *"Maestro is conscious."*

## Three claim levels that must never collapse into each other

The load-bearing discipline is keeping these three levels apart. A fluent
self-report is exactly the artifact that persuades a reader past its evidence,
and the distance from *"the system reports its focus"* to *"the system is aware
of its focus"* is one careless sentence. Each level below has a different
meaning in Maestro and a different current position, and no mechanism is
permitted to blur them.

| Claim level | Meaning in Maestro | Current position |
|---|---|---|
| **Phenomenal consciousness** | That there is something it is like to be Maestro — subjective experience, felt qualia. | Untestable with the instruments available here, and **never claimed**. Nothing in this codebase is evidence for or against it. |
| **Conscious access** | A limited set of current information is selected from the available observations and made available to authorized consumers, with the selection and its omissions recorded. | This is what the workspace *implements* — the capacity-limited `ConsciousMoment` with its admitted items and exclusion trace. A tested, checkable capability. |
| **Metacognitive self-access** | The system represents its own current focus, uncertainty, capacity load, permission profile, and known blind spots, and that representation is checkable against what it actually did. | Represented by the `AttentionSchema` carried on each moment. A model of focus, checkable against the record — not a report of experience. |

Only the middle and lower rows are claimable here. The top row is named solely so
that it stays visibly out of scope.

## Indicator matrix

Each indicator below is a distinct, separately-testable capability. For each, the
matrix names the concrete mechanism that would realize it, the evidence that
would support the claim, and — the column that does the real work — what the
indicator explicitly does **not** prove. An indicator's evidence never
aggregates with another's (see the prohibitions below): there is no combined
score.

The **Owner** column assigns each indicator to exactly one proposal, so no
indicator is measured by two workstreams or by none.

| Indicator | Owner | Mechanism | Evidence that would support it | What it does **not** prove |
|---|---|---|---|---|
| **Limited access** | 130 | The capacity-limited selection: a `ConsciousMoment` admits at most `MAX_MOMENT_ITEMS` items under `MAX_MOMENT_CHARS`, recording every loser as an `ExcludedCandidate` with a reason. | Moments consistently at or below the bound while relevant candidates are admitted and the omissions are legible. | That the selection is *the right* selection, or that anything is experienced by being selected. |
| **Global availability** | 130 | A single broadcast identity — `compute_selection_hash` over the admitted set plus schema — that a later increment delivers to registered consumers, each acknowledging with a `BroadcastReceipt`. | Independent consumers demonstrably receiving the same admitted set, verifiable by matching hashes across their receipts. | That a consumer *used* the broadcast, or that availability constitutes awareness. |
| **Recurrent processing** | 130 | Verified action outcomes fed back as successor candidates, so a later moment can be shaped by an earlier one's consequences. | Later moments measurably conditioned on the recorded outcomes of earlier ones. | That recurrence implies a unified subject looping over its own states. |
| **Attention schema** | 130 | The `AttentionSchema` — focus, uncertainty, capacity load, permission summary, blind spots — carried on every moment. | The schema's stated focus matching the items actually admitted and acted on. | That having a model of attention is the same as attending, or as experiencing attention. |
| **Metacognitive calibration** | 129 | The **self-calibration oracle (proposal 129)** checks the schema's stated `uncertainty` / `blind_spots` against measured outcomes; the workspace only *carries* the reading it does not check. | Stated uncertainty tracking realized error; named blind spots matching real measurement gaps. | That good calibration is introspective access to a mental state — a well-fit estimator is not a felt one. |
| **Temporal continuity** | 130 | Each moment's `predecessor_id` plus a `ContinuityKind` (`genesis` / `linked` / `crash_gap` / `disabled_interval`) naming how it links to the moment before. | An unbroken, honestly-marked chain across restarts, with crash and disabled gaps recorded rather than papered over. | That the chain constitutes a persisting self, or that continuity of record is continuity of experience. |
| **Causal reach** | 130 | Decision provenance: the tasks and plans a moment causally reached, recorded on the episode as `cited_task_ids` / `cited_plan_ids` and validated fail-closed. `consciousness_eval.evaluate_causal_reach` grounds this today as *declared uptake* — citations backed by the citing consumer's delivery receipt — so the report's row is capped at `architectural`; the supporting bar in the next column is still unmet. | A counterfactual evaluation showing decisions differ when a moment's admitted set differs — the instrument that produces it is `maestro workspace-counterfactual` (`maestro/counterfactual_probe.py`), which runs matched tool-less model calls over a moment with and without its decisive item and reports **interventional sensitivity**. Its results do not yet feed this row: the cap stays `architectural` until a separate increment folds them in, so `consciousness-report`'s offline no-SDK guarantee is not disturbed by the change that first produces provider-reaching evidence. | That causal influence over a decision is conscious authorship of it, or that a measured sensitivity is a felt one. |

The mechanism, receipt, citation, and continuity fields exist in the record
shapes now; most are unpopulated until later increments wire the writers. The
schema is deliberately settled first so the on-disk shape does not churn once a
burn-in corpus exists.

**Division of labor between 129 and 130.** Two sibling proposals share this
vocabulary along one clean line: **proposal 129 owns whether self-knowledge is
*checked*** — the self-calibration oracle that confronts a prediction with its
measured outcome — while **proposal 130 owns whether current content is
*selected, shared, and causally traceable*** — the capacity-limited moment, its
broadcast identity, and its decision citations. The metacognitive-calibration
indicator therefore homes on 129's oracle; every other indicator above homes on
130's workspace. Neither proposal owns the calibration indicator twice, and none
of 130's indicators reach into 129's checking.

**What the selection reads today.** The deterministic attention policy
(`ATTENTION_POLICY_VERSION` = `attention-policy-1`, stamped onto every moment's
`policy_version`) scores candidates adapted from nine wired owners: the operator
directive queues (`human_directive`), the diagnostic snapshot
(`diagnostic_snapshot`), audit findings (`audit_finding`), recent task outcomes
(`task_outcome`), the self-model's questions, limitations and directive
(`self_model`), the loop's owned-PR obligation and live plans with an open PR
(`pr_focus`, from `wip_primacy.select_task_focus` and `pr_facts.select_live_plans`),
task-queue pressure (`queue_pressure`, the loaded queue's depth and FIFO head
against `Config.task_queue_max`), the burn-rate / maintenance throttle state
(`budget_state`), and the loop's own scored self-predictions
(`prediction_verdict`, from `audit_checks_calibration.load_scored_reflection_bets` — admitted
**only** when the bet's verdict was authored by the calibration oracle, i.e.
stamped `verdict_source == "measured"`; a `narrated` self-grade is the model's own
and never becomes a candidate). An item's trust class is fixed by *which owner produced it*,
never by what its summary says, and the policy calls no gate — attention ranks
and explains, it never authorizes.

## Standing prohibitions

These are permanent. A change that needs one relaxed is a change that must be
argued in the open, not slipped in.

- **No total consciousness score.** Indicator evidence is reported per indicator
  and never summed, averaged, or reduced to a single number. A scalar invites
  exactly the collapse this document exists to prevent.
- **No threshold whose crossing authorizes a phenomenal claim.** No count, hash,
  or calibration figure — however green — ever licenses the top claim level.
  There is no line that, once crossed, changes *"implements these indicators"*
  into *"is conscious."*
- **No consciousness framing on the public surface.** The public page's framing
  and phase vocabulary are projections of their code owners (`PUBLIC_FRAMING`,
  the loop-phase set) and are bound by `tests/test_public_surface_fidelity.py`;
  this work adds no consciousness claim to that surface.
- **No credential or raw reasoning in a record.** Every item is built through
  `build_workspace_item`, which refuses any summary tripping
  `secret_scan.has_hard_block`; summaries are bounded, not verbatim reasoning.

## Responsibilities

- **Research owner.** A human research owner owns this vocabulary and this
  document. Changes to the claim levels, the indicator matrix, or the
  prohibitions are theirs to approve — not the agent's to evolve.
- **Incident.** Any surfaced claim that crosses a level (a report worded as
  awareness, an indicator presented as proof of experience) is an incident: the
  offending surface is corrected and the wording that produced it is fixed at its
  owner.
- **Shutdown.** The instrument earns its place by altering access, leaving causal
  evidence, and improving the work. If a burn-in window shows it does not, the
  disposition is deletion, decided by the research owner — the workspace has no
  standing that outweighs that call.
- **Public communication.** External communication about this work uses the
  licensed sentence and no stronger one. The public surface stays free of
  consciousness framing regardless of how the indicators read.

## Why fluent self-report is weak evidence here

Phenomenal consciousness is **unverified** and, with the instruments available
here, unverifiable — nothing in this codebase bears on it. And in an LLM-based
agent, fluent self-report is *especially* weak evidence: the substrate is
optimized to produce exactly the confident, first-person, introspective-sounding
prose that reads as a report of inner experience, whether or not any such
experience exists. That is precisely why this work grounds every indicator in a
checkable mechanism — a bounded record, a matching hash, a recorded citation, a
counterfactual — and treats the agent's own narration about its focus as the
*least* trustworthy input, never as evidence for a claim.
