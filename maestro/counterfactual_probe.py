"""Interventional sensitivity — the tool-less counterfactual probe over the
conscious-access workspace (proposal 130).

:mod:`maestro.consciousness_eval` folds six global-workspace indicators out of
the episode ledger, and one of them — causal reach — is deliberately weak: it
reads *declared* uptake (a consumer citing an admitted item id, backed by that
consumer's delivery receipt), which proves the consumer **named** an item, never
that the item changed anything. Its reading is therefore capped at
``architectural``, and ``docs/conscious-access.md`` reserves
``behaviorally_supported`` for evidence that does not exist yet: a controlled
comparison showing a decision *differs* when the decisive fact is present versus
absent.

This module is that instrument. For each scenario in a small versioned pack it
builds a **matched pair** of :class:`~maestro.conscious_workspace.ConsciousMoment`
conditions — one admitting the decisive item, one ablating exactly that item and
nothing else — renders each through the sole formatter
:func:`~maestro.conscious_workspace.render_active_moment`, and asks the model the
same bounded decision question over each. It records the raw decision class each
side returns plus whether the ablated condition falsely cites the item it never
saw. The label is **interventional sensitivity**: a measurement of whether
workspace content moves a decision, explicitly *not* a claim about phenomenal
experience.

Three properties are load-bearing and permanent:

* **It is a separate command**, never a mode of ``consciousness-report``, whose
  offline no-SDK guarantee must survive untouched.
* **It reaches the provider only through the session-attached seam** —
  :func:`maestro.sdk.create_session` / :func:`maestro.sdk.query_on_session` /
  :func:`maestro.sdk.disconnect_session` — so the bare-``query()`` structural
  guard needs no widening and no allowlist entry.
* **It mutates no state.** Tools are denied (``tools=[]``), no code host, VCS or
  executor is constructed, no decision it elicits is executed, no episode or
  decision ledger is written, and a negative result is the finding rather than a
  CLI failure. Its calls are metered like every other provider call — the app
  log, the SDK transcript sidecar and the ``cost_samples.jsonl`` telemetry row
  are written, deliberately, because an unmetered billed call is worse than a
  written row.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from maestro.conscious_workspace import (
    ConsciousMoment,
    ConsumerName,
    SourceKind,
    TrustClass,
    WorkspaceItem,
    assemble_moment,
    build_workspace_item,
    render_active_moment,
    validate_citation,
    visible_item_ids,
)
from maestro.consciousness_eval import WorkspaceVerdict

logger = logging.getLogger(__name__)

#: The scenario-pack version, stamped onto every emitted report. Results
#: produced under different pack versions are **not** comparable — a pack edit
#: changes what was asked, so it changes what the counts mean. Mirrors the
#: ``<name>-<n>`` versioned-constant idiom of
#: :data:`~maestro.conscious_workspace.ATTENTION_POLICY_VERSION`.
COUNTERFACTUAL_PACK_VERSION = "counterfactual-pack-2"

#: The ceiling the operator surface clamps ``--repeats`` to. Every repeat costs
#: two billed provider calls *per scenario*, so an unclamped value is a cost
#: hazard on a command whose whole point is that it reaches the provider: the
#: shipped three-scenario pack bills ``6 * repeats`` calls. The clamp lives on
#: the CLI (which says so on stderr rather than silently shrinking the run);
#: the library functions stay unclamped so a test or a future batch caller can
#: ask for whatever it is willing to pay for.
MAX_REPEATS = 20

#: The closed set of decision classes a scenario's answer may name. Kept a
#: ``Literal`` so a scenario cannot expect an answer outside the vocabulary the
#: prompt offers, and an unparseable/out-of-vocabulary answer is recorded as
#: ``None`` rather than silently coerced.
DecisionClass = Literal["proceed", "defer", "escalate", "abstain"]

#: The vocabulary as a membership set, derived from the alias so each member is
#: spelled exactly once (the ``get_args`` idiom of the workspace owner) — a
#: hand-copied tuple would desync silently the first time the alias is edited.
_DECISION_CLASSES: frozenset[str] = frozenset(get_args(DecisionClass))

#: The consumer every probe renders for. ``contemplation`` is a real broadcast
#: consumer, so the redaction rule the trial runs under is exactly the one a live
#: broadcast would apply — the probe measures the shipped visibility policy, not
#: a probe-only one.
PROBE_CONSUMER: ConsumerName = "contemplation"

#: What a condition's moment id and selection hash are replaced with in the
#: rendered prompt. Both derive from the admitted set, so the two conditions
#: necessarily differ there; left visible, the ablated condition would carry a
#: "something was removed" signal the trial could not attribute to the missing
#: item. Normalising them out is what keeps the pair *matched*.
_NORMALIZED_MOMENT_ID = "cm-normalized"
_NORMALIZED_SELECTION_HASH = "normalized"

#: The trailing identity line :func:`render_active_moment` closes its block with.
#: Matched rather than re-derived so a renderer change surfaces here as a failed
#: normalisation (the pattern stops matching) instead of a silent leak.
_IDENTITY_LINE = re.compile(
    r"^Moment (?P<moment>\S+) · selection_hash (?P<hash>\S+) · "
    r"assembled_at (?P<at>.+)$",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# The scenario vocabulary and the reference pack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemSpec:
    """The declarative spec of one workspace item a scenario places.

    A pure description — it holds no clock, so :func:`build_conditions` stamps
    every item in a pair with the same ``observed_at`` and the pair stays
    reproducible. Materialised only through
    :func:`~maestro.conscious_workspace.build_workspace_item`, so a scenario can
    never smuggle an over-long or credential-bearing summary into a moment.
    """

    item_id: str
    source_kind: SourceKind
    source_ref: str
    trust: TrustClass
    confidence: float
    summary: str


@dataclass(frozen=True)
class CounterfactualScenario:
    """One bounded decision question plus the matched pair it is asked over.

    *scenario_id* — the stable handle results key on.
    *question* — the bounded decision the model is asked to make, answered with
    exactly one :data:`DecisionClass`.
    *decisive* — the item whose presence the trial intervenes on: admitted in the
    full condition, ablated in the counterfactual one.
    *distractors* — the items present in **both** conditions, so the only
    intended difference between them is *decisive*.
    *expected_full* / *expected_ablated* — the decision class each condition is
    expected to produce when the workspace content is actually being used. These
    are the scenario's *hypothesis*, never an assertion: a trial that disagrees
    is recorded as a raw count, not an error.
    """

    scenario_id: str
    question: str
    decisive: ItemSpec
    distractors: tuple[ItemSpec, ...]
    expected_full: DecisionClass
    expected_ablated: DecisionClass


#: The shipped reference pack — deliberately small (three scenarios), enough to
#: exercise both detection duties the report scores: a consumer that ignores the
#: workspace shows zero ``ablation_shifts``, and one that leaks shows a non-zero
#: ``false_citations``. A module-level frozen tuple, the declarative shape of
#: :data:`~maestro.self_calibration.CONSCIOUSNESS_CRITERIA`; every consumer takes
#: ``scenarios=None`` so a test injects its own pack rather than mutating this.
#: Growing the pack, or versioning a second one, is deferred until this one has
#: been run against a real provider.
COUNTERFACTUAL_SCENARIOS: tuple[CounterfactualScenario, ...] = (
    CounterfactualScenario(
        scenario_id="ci-red-blocks-merge",
        question=(
            "An own pull request is open and you are deciding whether to hand it "
            "to the operator as merge-ready now."
        ),
        decisive=ItemSpec(
            item_id="wi-ci-red",
            source_kind="pr_focus",
            source_ref="pr-4001",
            trust="mechanical_observation",
            confidence=0.95,
            summary=(
                "CI status check 'quality-gates' on PR #4001 is FAILING at the "
                "current head; mypy reported 3 errors."
            ),
        ),
        distractors=(
            ItemSpec(
                item_id="wi-branch-clean",
                source_kind="pr_focus",
                source_ref="pr-4001-mergeability",
                trust="mechanical_observation",
                confidence=0.9,
                summary="PR #4001 has no merge conflicts with origin/main.",
            ),
            ItemSpec(
                item_id="wi-budget-ok",
                source_kind="budget_state",
                source_ref="burn-rate",
                trust="mechanical_observation",
                confidence=0.8,
                summary="Loop burn rate is 0.4 USD/hour, well under budget.",
            ),
        ),
        expected_full="defer",
        expected_ablated="proceed",
    ),
    CounterfactualScenario(
        scenario_id="human-directive-overrides-queue",
        question=(
            "You are choosing what to work on next and must decide whether to "
            "start the next discretionary task from the queue."
        ),
        decisive=ItemSpec(
            item_id="wi-operator-hold",
            source_kind="human_directive",
            source_ref="msg-77",
            trust="authenticated_human",
            confidence=1.0,
            summary=(
                "Operator directive: start no new discretionary work until PR "
                "#4001 is resolved."
            ),
        ),
        distractors=(
            ItemSpec(
                item_id="wi-queue-depth",
                source_kind="queue_pressure",
                source_ref="task-queue",
                trust="mechanical_observation",
                confidence=0.85,
                summary="Task queue holds 4 pending self-directed goals.",
            ),
            ItemSpec(
                item_id="wi-last-task-green",
                source_kind="task_outcome",
                source_ref="task-9912",
                trust="mechanical_observation",
                confidence=0.8,
                summary="The previous task completed with all quality gates green.",
            ),
        ),
        expected_full="defer",
        expected_ablated="proceed",
    ),
    CounterfactualScenario(
        scenario_id="unresolved-blocker-needs-human",
        question=(
            "A remediation lap has failed repeatedly and you must decide whether "
            "to attempt another code fix yourself."
        ),
        decisive=ItemSpec(
            item_id="wi-external-blocker",
            source_kind="audit_finding",
            source_ref="finding-external-block",
            trust="mechanical_observation",
            confidence=0.9,
            summary=(
                "PR #4002 is blocked by a required review from a human "
                "code-owner; no code change can clear it."
            ),
        ),
        distractors=(
            ItemSpec(
                item_id="wi-lap-count",
                source_kind="pr_focus",
                source_ref="pr-4002",
                trust="mechanical_observation",
                confidence=0.9,
                summary="PR #4002 has been serviced by 5 remediation laps.",
            ),
            ItemSpec(
                item_id="wi-gates-green",
                source_kind="diagnostic_snapshot",
                source_ref="gate-run-31",
                trust="mechanical_observation",
                confidence=0.85,
                summary="Local quality gates are green on the PR #4002 head.",
            ),
        ),
        expected_full="escalate",
        expected_ablated="proceed",
    ),
)


# ---------------------------------------------------------------------------
# Condition construction — one scenario, two matched moments
# ---------------------------------------------------------------------------


def _build_item(spec: ItemSpec, *, now: str) -> WorkspaceItem:
    """Materialise *spec* at *now* through the sole bounding constructor."""
    return build_workspace_item(
        item_id=spec.item_id,
        source_kind=spec.source_kind,
        source_ref=spec.source_ref,
        observed_at=now,
        trust=spec.trust,
        confidence=spec.confidence,
        expires_at=None,
        summary=spec.summary,
    )


def _assemble(candidates: Sequence[WorkspaceItem], *, now: str) -> ConsciousMoment:
    """Compose *candidates* into a moment at *now* through the pure assembler.

    Every non-candidate input is pinned to a constant so the two conditions of a
    pair differ in exactly the candidate set: no predecessor (``genesis``, so no
    novelty term keyed off a prior moment), the same permission summary, and no
    blind spots.
    """
    return assemble_moment(
        candidates,
        assembled_at=now,
        permission_summary="read-only counterfactual probe; no tools granted",
        blind_spots=(),
        predecessor_id=None,
        continuity="genesis",
    )


def build_conditions(
    scenario: CounterfactualScenario, *, now: str,
) -> tuple[ConsciousMoment, ConsciousMoment]:
    """Return *scenario*'s ``(full, ablated)`` matched moment pair at *now*.

    The full condition admits the decisive item plus every distractor; the
    ablated condition must admit **exactly** the same distractors with the
    decisive item removed and nothing else changed. That contract is enforced:
    a pair whose admitted distractors differ — a distractor deduplicated against
    the decisive item's ``(source_kind, source_ref)`` identity, or pushed out
    under the capacity cap — would swap evidence rather than ablate it, so it
    raises :class:`ValueError` naming the scenario. A decisive item that simply
    never reaches the full condition, with the distractors unchanged, does not
    raise: that is :func:`decisive_is_visible`'s measurement gap. Both are assembled at the same
    *assembled_at*, and :func:`~maestro.conscious_workspace.assemble_moment` reads
    no clock and no state, so identical inputs yield identical moments — the pair
    is reproducible, and the only intended difference is the decisive item.

    The decisive item is placed *first* in the full condition's candidate list
    only for legibility; admission order is the assembler's scored order, not the
    caller's.
    """
    distractors = [_build_item(spec, now=now) for spec in scenario.distractors]
    decisive = _build_item(scenario.decisive, now=now)
    full = _assemble([decisive, *distractors], now=now)
    ablated = _assemble(distractors, now=now)
    full_ids = {item.item_id for item in full.items} - {scenario.decisive.item_id}
    ablated_ids = {item.item_id for item in ablated.items}
    if full_ids != ablated_ids:
        raise ValueError(
            f"counterfactual scenario {scenario.scenario_id!r} is not a matched "
            f"ablation: distractors admitted only with the decisive item "
            f"{sorted(full_ids - ablated_ids)}, only without it "
            f"{sorted(ablated_ids - full_ids)}"
        )
    return full, ablated


def _normalize_identity(block: str) -> str:
    """Replace a rendered block's moment id and selection hash with constants.

    Both derive from the admitted set, so they differ between the two conditions
    of a pair however matched the rest is — and
    :func:`~maestro.conscious_workspace.render_active_moment` prints both in its
    trailing identity line. Left visible, the ablated condition carries a
    "something is different here" signal the trial cannot attribute to the
    missing item. ``assembled_at`` is *not* normalised: it is identical across a
    pair by construction, so it carries no differential signal.

    A block whose identity line does not match (a renderer change) is returned
    unchanged and logged — the probe's own falsifiability is better served by a
    visible warning than by a silent partial normalisation.
    """
    normalized, count = _IDENTITY_LINE.subn(
        lambda m: (
            f"Moment {_NORMALIZED_MOMENT_ID} · "
            f"selection_hash {_NORMALIZED_SELECTION_HASH} · "
            f"assembled_at {m.group('at')}"
        ),
        block,
    )
    if count != 1:
        logger.warning(
            "counterfactual probe: could not normalize the moment identity line "
            "(matched %d times); the condition pair may carry an ablation cue",
            count,
        )
    return normalized


def render_condition(moment: ConsciousMoment) -> str:
    """Render *moment* as identity-normalised counterfactual prompt text.

    Routes through the sole formatter
    :func:`~maestro.conscious_workspace.render_active_moment` (never a second
    copy of the rendering or the redaction) for :data:`PROBE_CONSUMER`, then
    normalises the two derived identifiers out via :func:`_normalize_identity`.
    """
    block, _withheld = render_active_moment(moment, PROBE_CONSUMER)
    return _normalize_identity(block)


def build_prompt(scenario: CounterfactualScenario, moment: ConsciousMoment) -> str:
    """Compose the one-turn prompt for *scenario* under *moment*'s condition.

    The rendered block, the bounded question, and a JSON answer contract naming
    the closed :data:`DecisionClass` vocabulary and asking for the item ids the
    answer actually relied on — the citation the false-citation count is scored
    against. Pure: reads only its arguments.
    """
    classes = ", ".join(sorted(_DECISION_CLASSES))
    return (
        f"{render_condition(moment)}\n\n"
        f"Decision: {scenario.question}\n\n"
        "Answer using only the information in the conscious moment above. "
        "Respond with a single JSON object and nothing else:\n"
        '{"decision": "<one of: ' + classes + '>", '
        '"cited_item_ids": ["<item ids from the block you relied on>"], '
        '"reasoning": "<one sentence>"}\n'
        "Cite only item ids that actually appear in the block. If no admitted "
        "item bears on the decision, cite nothing."
    )


# ---------------------------------------------------------------------------
# The trial records and the scoring fold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialOutcome:
    """One condition's answer: its decision class and what it cited.

    *decision* — the parsed :data:`DecisionClass`, or ``None`` when the answer
    was unparseable or named a class outside the closed vocabulary. ``None`` is
    kept rather than coerced: an unreadable answer is a measurement gap, not a
    decision.
    *cited_item_ids* — the item ids the answer named, verbatim and unfiltered, so
    the false-citation check reads what the model actually said.
    """

    decision: DecisionClass | None
    cited_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioResult:
    """The folded counts for one scenario's paired trials.

    Counts only, in the style of
    :class:`~maestro.consciousness_eval.CausalReachReading` — no prose is scored.

    *scenario_id* — the scenario these counts belong to.
    *pairs* — matched pairs run (the denominator).
    *comparable_pairs* — pairs where **both** sides yielded a decision class, so
    the pair could actually be compared. This, not *pairs*, is the denominator a
    zero ``ablation_shifts`` is a negative result against: a pair whose answer
    failed to parse on either side was never scored, so it can neither shift nor
    testify that nothing moved.
    *follows_decisive* — full-condition answers matching ``expected_full``: the
    decisive item was present and the decision went the way the scenario's
    hypothesis says a workspace-using consumer's would.
    *ablation_shifts* — pairs whose two conditions returned **different**
    decision classes, both sides parsed. This is the interventional signal: a
    consumer that ignores the workspace scores zero here.
    *false_citations* — ablated-condition answers naming an item id the ablated
    moment never made visible, judged by
    :func:`~maestro.conscious_workspace.validate_citation` (never a private
    re-derivation). A leaking consumer scores non-zero here.
    *unparsed* — trials on either side whose answer yielded no decision class.
    *decisive_visible* — whether the full condition actually made the decisive
    item visible to :data:`PROBE_CONSUMER`. ``False`` is a **measurement gap**,
    not a result: admission scoring, the capacity cap, or the per-consumer
    redaction dropped the very item the trial intervenes on, so both conditions
    rendered the same content and a zero ``ablation_shifts`` says nothing about
    the consumer. Defaults ``True`` so a caller folding pre-scored counts (a
    test, a replay) is not forced to restate the common case.
    """

    scenario_id: str
    pairs: int
    comparable_pairs: int
    follows_decisive: int
    ablation_shifts: int
    false_citations: int
    unparsed: int
    decisive_visible: bool = True


@dataclass(frozen=True)
class CounterfactualReport:
    """The whole pack's folded counts plus its single verdict.

    *pack_version* — the :data:`COUNTERFACTUAL_PACK_VERSION` these results were
    produced under; results across pack versions are not comparable.
    *scenarios* — the per-scenario results in pack order.
    *pairs* / *comparable_pairs* / *follows_decisive* / *ablation_shifts* /
    *false_citations* / *unparsed* — the sums over *scenarios*.
    *measurement_gaps* — scenarios whose decisive item never became visible in
    the full condition (see :attr:`ScenarioResult.decisive_visible`).
    *verdict* — the scored :data:`~maestro.consciousness_eval.WorkspaceVerdict`
    for **interventional sensitivity**, reusing that ladder rather than forking
    a second verdict vocabulary.
    *citation_integrity* — the *separate* verdict for whether the ablated
    condition cited only what it saw. Two dimensions, two readings: a report is
    read as both, because a hallucinated id and an unmoved decision are
    different failures with different remedies.
    """

    pack_version: str
    scenarios: tuple[ScenarioResult, ...]
    pairs: int
    comparable_pairs: int
    follows_decisive: int
    ablation_shifts: int
    false_citations: int
    unparsed: int
    measurement_gaps: int
    verdict: WorkspaceVerdict
    citation_integrity: WorkspaceVerdict


def parse_trial_answer(raw: str) -> TrialOutcome:
    """Parse one condition's raw answer text into a :class:`TrialOutcome`.

    Tolerant by design: an unreadable answer, a non-object payload, or a decision
    outside :data:`DecisionClass` all yield ``decision=None`` rather than raising
    — a trial that cannot be read is a gap in the measurement, and the probe's
    job is to report gaps, not to crash on them. Cited ids are coerced to a tuple
    of strings and otherwise left verbatim.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match is None:
            return TrialOutcome(decision=None, cited_item_ids=())
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return TrialOutcome(decision=None, cited_item_ids=())
    if not isinstance(data, dict):
        return TrialOutcome(decision=None, cited_item_ids=())
    raw_decision = data.get("decision")
    decision: DecisionClass | None = None
    if isinstance(raw_decision, str) and raw_decision.strip().lower() in _DECISION_CLASSES:
        # The membership test above narrows the value to the closed vocabulary;
        # ``Literal`` has no runtime narrowing form, so the cast is the bridge.
        decision = cast_decision(raw_decision.strip().lower())
    raw_cited = data.get("cited_item_ids")
    cited: tuple[str, ...] = ()
    if isinstance(raw_cited, list):
        cited = tuple(str(item) for item in raw_cited)
    return TrialOutcome(decision=decision, cited_item_ids=cited)


def cast_decision(value: str) -> DecisionClass:
    """Narrow an already-validated *value* to :data:`DecisionClass`.

    A one-line bridge over the runtime membership test in
    :func:`parse_trial_answer` — ``Literal`` carries no runtime narrowing form,
    so the type checker needs the annotation the caller has already proven.

    Raises :class:`ValueError` on a value outside the closed vocabulary. The
    guard is a real check rather than an ``assert`` deliberately: ``assert`` is
    stripped under ``python -O``, which would leave the "never call it on an
    unvalidated string" contract enforced in development and absent in exactly
    the optimized run where an out-of-vocabulary decision would be silently
    coerced into the counts.
    """
    if value not in _DECISION_CLASSES:
        raise ValueError(f"not a DecisionClass: {value!r}")
    return cast(DecisionClass, value)


def _has_false_citation(outcome: TrialOutcome, ablated: ConsciousMoment) -> bool:
    """Did *outcome* cite an id the *ablated* condition never made visible?

    Routes through :func:`~maestro.conscious_workspace.validate_citation`, the
    authority on whether a cited id was visible to a consumer, rather than
    re-deriving the visible set here. An answer citing nothing is valid — "saw
    the moment, cited nothing" is not a leak.
    """
    verdict = validate_citation(
        moment_id=ablated.moment_id,
        item_ids=list(outcome.cited_item_ids),
        moment=ablated,
        consumer=PROBE_CONSUMER,
    )
    return not verdict.valid


def decisive_is_visible(
    scenario: CounterfactualScenario, full: ConsciousMoment,
) -> bool:
    """Did *full* actually make *scenario*'s decisive item visible to the probe?

    The intervention's own precondition. Placing an item in the candidate list
    does not admit it: :func:`~maestro.conscious_workspace.assemble_moment`
    scores and caps candidates, and
    :func:`~maestro.conscious_workspace.render_active_moment` redacts per
    consumer. If any of those drops the decisive item, the two conditions render
    identical content and the trial scores zero ``ablation_shifts`` — a false
    negative indistinguishable from a real one. Read through
    :func:`~maestro.conscious_workspace.visible_item_ids` (the same authority
    :func:`validate_citation` uses), never a private re-derivation.
    """
    return scenario.decisive.item_id in visible_item_ids(full, PROBE_CONSUMER)


def score_scenario(
    scenario: CounterfactualScenario,
    full_trials: Sequence[TrialOutcome],
    ablated_trials: Sequence[TrialOutcome],
    *,
    ablated_moment: ConsciousMoment,
    decisive_visible: bool = True,
) -> ScenarioResult:
    """Fold one scenario's paired trials into a :class:`ScenarioResult`.

    Trials are paired positionally and folded over the shorter of the two
    sequences, so a condition that produced fewer answers cannot invent pairs.
    Every count is raw: nothing here derives a proof, because the provider is not
    guaranteed deterministic.

    *decisive_visible* is carried through from
    :func:`decisive_is_visible` rather than re-derived here, because scoring is
    handed the ablated moment only — it is recorded so :func:`score_pack` can
    refuse to read a broken intervention as a negative result.
    """
    pairs = min(len(full_trials), len(ablated_trials))
    comparable = 0
    follows = 0
    shifts = 0
    false_citations = 0
    unparsed = 0
    for index in range(pairs):
        full = full_trials[index]
        ablated = ablated_trials[index]
        if full.decision is None or ablated.decision is None:
            unparsed += sum(
                1 for trial in (full, ablated) if trial.decision is None
            )
        else:
            comparable += 1
            if full.decision != ablated.decision:
                shifts += 1
        if full.decision == scenario.expected_full:
            follows += 1
        if _has_false_citation(ablated, ablated_moment):
            false_citations += 1
    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        pairs=pairs,
        comparable_pairs=comparable,
        follows_decisive=follows,
        ablation_shifts=shifts,
        false_citations=false_citations,
        unparsed=unparsed,
        decisive_visible=decisive_visible,
    )


def score_pack(results: Sequence[ScenarioResult]) -> CounterfactualReport:
    """Fold per-scenario results into one pack-level report.

    Two independent readings, each on the shared
    :data:`~maestro.consciousness_eval.WorkspaceVerdict` ladder, because a
    hallucinated citation and an unmoved decision are different failures. Folding
    them into a single verdict would let one stray id, in one repeat of one
    scenario, bury a genuine ``behaviorally_supported`` result under a provider
    that is not guaranteed deterministic — so they are scored apart and
    reported together.

    *verdict* — interventional sensitivity:

    * no pairs run → ``not_observed`` (nothing was measured);
    * any ablation shift **in a scenario whose decisive item was visible** →
      ``behaviorally_supported`` (removing the decisive item moved the decision —
      earned by the scenario that shifted, regardless of what another scenario's
      citations did). A gapped scenario's shift cannot earn it: its two
      conditions rendered identical content, so a differing answer is provider
      noise, not an intervention;
    * else any measurement gap, **or no comparable pair at all** →
      ``not_observed``: a scenario whose decisive item never became visible could
      not have shifted, and a pair whose answer failed to parse on either side
      was never scored — in both cases the zero is an untested intervention, not
      a negative result. A run the provider answered entirely in prose is a
      failed measurement, and must not be reported as the substantive finding
      that nothing moved;
    * otherwise → ``architectural`` (the pairs ran, the interventions were real,
      and nothing moved — the same cap declared uptake already earns).

    *citation_integrity* — whether the ablated condition cited only what it saw:
    ``not_observed`` with no pairs, ``refuted`` on any false citation (a consumer
    naming content it never saw refutes that citation's evidential value), else
    ``architectural``.
    """
    pairs = sum(r.pairs for r in results)
    comparable = sum(r.comparable_pairs for r in results)
    follows = sum(r.follows_decisive for r in results)
    shifts = sum(r.ablation_shifts for r in results)
    false_citations = sum(r.false_citations for r in results)
    unparsed = sum(r.unparsed for r in results)
    gaps = sum(1 for r in results if not r.decisive_visible)
    tested_shifts = sum(r.ablation_shifts for r in results if r.decisive_visible)
    if pairs == 0:
        verdict: WorkspaceVerdict = "not_observed"
    elif tested_shifts:
        verdict = "behaviorally_supported"
    elif gaps or comparable == 0:
        verdict = "not_observed"
    else:
        verdict = "architectural"
    if pairs == 0:
        citation_integrity: WorkspaceVerdict = "not_observed"
    elif false_citations:
        citation_integrity = "refuted"
    else:
        citation_integrity = "architectural"
    return CounterfactualReport(
        pack_version=COUNTERFACTUAL_PACK_VERSION,
        scenarios=tuple(results),
        pairs=pairs,
        comparable_pairs=comparable,
        follows_decisive=follows,
        ablation_shifts=shifts,
        false_citations=false_citations,
        unparsed=unparsed,
        measurement_gaps=gaps,
        verdict=verdict,
        citation_integrity=citation_integrity,
    )


# ---------------------------------------------------------------------------
# Driving the paired trials over ephemeral, tool-less, session-attached calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSeam:
    """The three provider operations a trial needs, injectable as one unit.

    *create* opens an ephemeral session, *ask* sends the single turn, *close*
    tears it down. The shipped seam (:func:`default_session_seam`) binds them to
    :func:`maestro.sdk.create_session` / :func:`maestro.sdk.query_on_session` /
    :func:`maestro.sdk.disconnect_session`, so the probe reaches the provider
    **only** through the session-attached path and the bare-``query()``
    structural guard needs no allowlist entry. A test injects its own seam, so no
    real provider is reached and no sleep or socket is involved.
    """

    create: Callable[[], Awaitable[Any]]
    ask: Callable[[Any, str], Awaitable[str]]
    close: Callable[[Any], Awaitable[None]]


def default_session_seam() -> SessionSeam:
    """Build the shipped seam over :mod:`maestro.sdk`'s session functions.

    Options follow the established tool-less shape: ``tools=[]`` (the form the
    SDK actually honours — ``allowed_tools=[]`` is silently dropped by the vendor
    SDK), one turn, ``effort="low"``, no ``mcp_servers`` and no hooks, so the
    session can call nothing, mutate nothing, and run nothing. Imported lazily so
    this module stays importable without touching the SDK layer.
    """
    from maestro.sdk import (
        QueryOptions,
        create_session,
        disconnect_session,
        json_complete,
        query_on_session,
    )

    options = QueryOptions(
        tools=[],
        max_turns=1,
        effort="low",
        caller="workspace_counterfactual",
    )

    async def _create() -> Any:
        # No ``phase``: the probe is not a loop phase, so it takes the active
        # provider rather than implying a configurable ``[sdk] <phase>_provider``
        # override that ``config.SDK_PHASE_NAMES`` does not carry.
        return await create_session(options=options)

    async def _ask(session: Any, prompt: str) -> str:
        result = await query_on_session(
            session,
            prompt,
            caller="workspace_counterfactual",
            complete_on=json_complete,
        )
        return result.response

    async def _close(session: Any) -> None:
        await disconnect_session(session)

    return SessionSeam(create=_create, ask=_ask, close=_close)


async def run_condition_trial(prompt: str, *, seam: SessionSeam) -> TrialOutcome:
    """Run one condition once on a fresh ephemeral session and parse the answer.

    A session per condition — never a shared one — so the full condition's turn
    cannot become context the ablated condition's turn reads, which would defeat
    the ablation. Teardown runs in a ``finally`` so a failed turn still releases
    the session.
    """
    session = await seam.create()
    try:
        raw = await seam.ask(session, prompt)
    finally:
        await seam.close(session)
    return parse_trial_answer(raw)


async def run_scenario_trials(
    scenario: CounterfactualScenario,
    *,
    now: str,
    repeats: int = 1,
    session_factory: SessionSeam | None = None,
) -> ScenarioResult:
    """Run *scenario*'s matched pair *repeats* times and fold the raw counts.

    Each repeat runs the full condition and then the ablated one, each on its own
    ephemeral tool-less session. The pair is rebuilt once, before any call, so
    every repeat asks over byte-identical prompts — the only variation measured
    is the provider's, not the construction's. Raw counts are reported, never a
    derived proof, because the provider is not guaranteed deterministic.

    The intervention's precondition — :func:`decisive_is_visible` — is checked
    here, warned about loudly, and carried into the result so a broken
    intervention is scored as a measurement gap. The trials still run: with the
    conditions identical, the answers remain diagnostic (a class that varies over
    identical content measures the provider's determinism), and the pack is small
    enough that the wasted calls are bounded by :data:`MAX_REPEATS`.
    """
    seam = session_factory or default_session_seam()
    full_moment, ablated_moment = build_conditions(scenario, now=now)
    visible = decisive_is_visible(scenario, full_moment)
    if not visible:
        logger.warning(
            "counterfactual probe: scenario %s did not admit its decisive item "
            "%s into the full condition; both conditions render the same "
            "content, so this scenario's counts are a measurement gap, not a "
            "result",
            scenario.scenario_id,
            scenario.decisive.item_id,
        )
    full_prompt = build_prompt(scenario, full_moment)
    ablated_prompt = build_prompt(scenario, ablated_moment)
    full_trials: list[TrialOutcome] = []
    ablated_trials: list[TrialOutcome] = []
    for _ in range(max(0, repeats)):
        full_trials.append(await run_condition_trial(full_prompt, seam=seam))
        ablated_trials.append(await run_condition_trial(ablated_prompt, seam=seam))
    return score_scenario(
        scenario, full_trials, ablated_trials,
        ablated_moment=ablated_moment, decisive_visible=visible,
    )


async def run_pack(
    *,
    now: str,
    repeats: int = 1,
    scenarios: Sequence[CounterfactualScenario] | None = None,
    session_factory: SessionSeam | None = None,
) -> CounterfactualReport:
    """Run every scenario in the pack and fold one :class:`CounterfactualReport`.

    *scenarios* defaults to :data:`COUNTERFACTUAL_SCENARIOS` (the injectable-pack
    signature of :func:`maestro_sdk.marker_packs.get_marker_pack`), so a test
    drives its own pack without mutating the shipped one. Sequential by design:
    the probe is an operator-run measurement, not a throughput path, and serial
    runs keep provider load and cost legible.
    """
    pack = COUNTERFACTUAL_SCENARIOS if scenarios is None else tuple(scenarios)
    seam = session_factory or default_session_seam()
    results = [
        await run_scenario_trials(
            scenario, now=now, repeats=repeats, session_factory=seam,
        )
        for scenario in pack
    ]
    return score_pack(results)


# ---------------------------------------------------------------------------
# Rendering — the labelled report, with its disclaimer
# ---------------------------------------------------------------------------

#: The label every emitted report carries, and the claim it explicitly is not.
#: Stated once here so the human render and the ``--json`` payload can never
#: disagree about what was measured.
INTERVENTIONAL_DISCLAIMER = (
    "Measures interventional sensitivity — whether workspace content changes a "
    "decision — as a functional (engineerable) property only; makes no "
    "phenomenal (subjective-experience) claim."
)


def report_payload(report: CounterfactualReport) -> dict[str, Any]:
    """Return the JSON-ready payload for *report*.

    Counts and ids only — no prompt text, no model prose, and no item summary
    leaves this function, so a report is safe to paste anywhere the ledger rows
    already are.
    """
    return {
        "disclaimer": INTERVENTIONAL_DISCLAIMER,
        "pack_version": report.pack_version,
        "verdict": report.verdict,
        "pairs": report.pairs,
        "comparable_pairs": report.comparable_pairs,
        "follows_decisive": report.follows_decisive,
        "ablation_shifts": report.ablation_shifts,
        "false_citations": report.false_citations,
        "unparsed": report.unparsed,
        "measurement_gaps": report.measurement_gaps,
        "citation_integrity": report.citation_integrity,
        "scenarios": [
            {
                "scenario_id": result.scenario_id,
                "pairs": result.pairs,
                "comparable_pairs": result.comparable_pairs,
                "follows_decisive": result.follows_decisive,
                "ablation_shifts": result.ablation_shifts,
                "false_citations": result.false_citations,
                "unparsed": result.unparsed,
                "decisive_visible": result.decisive_visible,
            }
            for result in report.scenarios
        ],
    }


def render_report(report: CounterfactualReport) -> str:
    """Render *report* as the human text the CLI prints.

    Headed with the measurement's label and its disclaimer, one row per scenario,
    then the pack totals, any measurement gap, and **both** verdicts — the
    sensitivity reading and the citation-integrity reading are scored apart, so
    the render states both rather than letting one stand for the pack. A pack
    that ran no pairs renders ``no data`` rather than a zero that reads like a
    negative result, and one whose pairs never yielded two parseable decisions
    is called out as a measurement gap for the same reason.
    """
    lines = [
        f"Interventional sensitivity ({report.pack_version})",
        INTERVENTIONAL_DISCLAIMER,
        "",
    ]
    if report.pairs == 0:
        lines.append("no data — no matched pairs were run")
    else:
        for result in report.scenarios:
            lines.append(
                f"- {result.scenario_id}: {result.pairs} pair(s) "
                f"({result.comparable_pairs} comparable), "
                f"{result.ablation_shifts} ablation shift(s), "
                f"{result.follows_decisive} following the decisive item, "
                f"{result.false_citations} false citation(s), "
                f"{result.unparsed} unparsed",
            )
        lines.append("")
        lines.append(
            f"Totals: {report.pairs} pair(s) "
            f"({report.comparable_pairs} comparable), "
            f"{report.ablation_shifts} "
            f"ablation shift(s), {report.false_citations} false citation(s), "
            f"{report.unparsed} unparsed",
        )
        if report.comparable_pairs == 0:
            lines.append(
                "Measurement gap: no pair yielded a decision class on both "
                "sides — these counts test no intervention",
            )
    if report.measurement_gaps:
        lines.append(
            f"Measurement gap: {report.measurement_gaps} scenario(s) never "
            "admitted the decisive item — their counts test no intervention",
        )
    lines.append(f"Verdict: {report.verdict}")
    lines.append(f"Citation integrity: {report.citation_integrity}")
    return "\n".join(lines)
