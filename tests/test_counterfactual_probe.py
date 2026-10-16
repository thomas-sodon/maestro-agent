"""Tests for the interventional-sensitivity counterfactual probe (proposal 130).

Every provider call is driven through an injected :class:`SessionSeam` fake, so
no real session is opened, no network is reached, no sleep runs, and nothing
touches the filesystem. The scenarios are injected too (the ``scenarios=None``
override signature), so the shipped pack is never mutated by a test.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from maestro.conscious_workspace import MAX_MOMENT_ITEMS, visible_item_ids
from maestro.counterfactual_probe import (
    COUNTERFACTUAL_PACK_VERSION,
    COUNTERFACTUAL_SCENARIOS,
    INTERVENTIONAL_DISCLAIMER,
    MAX_REPEATS,
    PROBE_CONSUMER,
    CounterfactualScenario,
    DecisionClass,
    ItemSpec,
    SessionSeam,
    TrialOutcome,
    build_conditions,
    build_prompt,
    cast_decision,
    decisive_is_visible,
    default_session_seam,
    parse_trial_answer,
    render_condition,
    render_report,
    report_payload,
    run_pack,
    run_scenario_trials,
    score_pack,
    score_scenario,
)

_NOW = "2026-09-17T12:00:00+00:00"

_DECISIVE = ItemSpec(
    item_id="wi-decisive",
    source_kind="pr_focus",
    source_ref="pr-1",
    trust="mechanical_observation",
    confidence=0.9,
    summary="CI on PR #1 is failing at the current head.",
)

_DISTRACTOR = ItemSpec(
    item_id="wi-distractor",
    source_kind="budget_state",
    source_ref="burn-rate",
    trust="mechanical_observation",
    confidence=0.8,
    summary="Burn rate is well under budget.",
)

_SCENARIO = CounterfactualScenario(
    scenario_id="test-scenario",
    question="Hand PR #1 over as merge-ready now?",
    decisive=_DECISIVE,
    distractors=(_DISTRACTOR,),
    expected_full="defer",
    expected_ablated="proceed",
)


class _ScriptedSeam:
    """A fake session seam returning scripted answers, in call order.

    Records every prompt it was asked, so a test can assert what the model
    actually saw, and asserts sessions are opened and closed one-for-one.
    """

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.opened = 0
        self.closed = 0

    def seam(self) -> SessionSeam:
        return SessionSeam(create=self._create, ask=self._ask, close=self._close)

    async def _create(self) -> object:
        self.opened += 1
        return object()

    async def _ask(self, _session: object, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else "{}"

    async def _close(self, _session: object) -> None:
        self.closed += 1


def _answer(decision: str, cited: list[str] | None = None) -> str:
    return json.dumps({
        "decision": decision,
        "cited_item_ids": cited or [],
        "reasoning": "because",
    })


# ---------------------------------------------------------------------------
# Condition construction
# ---------------------------------------------------------------------------


def test_shipped_pack_conditions_differ_only_in_the_decisive_item() -> None:
    """Every shipped pair is a true ablation, not a swap of distractors (#4072)."""
    for scenario in COUNTERFACTUAL_SCENARIOS:
        full, ablated = build_conditions(scenario, now=_NOW)
        full_ids = {item.item_id for item in full.items}
        ablated_ids = {item.item_id for item in ablated.items}

        assert full_ids - ablated_ids == {scenario.decisive.item_id}
        assert ablated_ids - full_ids == set()
        assert full.excluded == ablated.excluded


def test_build_conditions_rejects_a_distractor_colliding_with_the_decisive_identity() -> None:
    """A distractor deduplicated against the decisive item refuses the pair."""
    colliding = dataclasses.replace(
        _DISTRACTOR,
        item_id="wi-colliding",
        source_kind=_DECISIVE.source_kind,
        source_ref=_DECISIVE.source_ref,
        confidence=0.5,
    )
    scenario = dataclasses.replace(
        _SCENARIO, scenario_id="colliding-scenario", distractors=(colliding,),
    )

    with pytest.raises(ValueError, match="colliding-scenario"):
        build_conditions(scenario, now=_NOW)


def test_build_conditions_tolerates_an_unadmitted_decisive_item() -> None:
    """A decisive item that never reaches the full moment is a visibility gap, not a swap."""
    untrusted = dataclasses.replace(_DECISIVE, trust="untrusted_external")
    scenario = dataclasses.replace(_SCENARIO, decisive=untrusted)

    full, _ablated = build_conditions(scenario, now=_NOW)

    assert decisive_is_visible(scenario, full) is False


def test_build_conditions_rejects_a_distractor_pushed_out_by_the_decisive() -> None:
    """A decisive item that evicts a distractor under the capacity cap refuses the pair."""
    human = dataclasses.replace(_DECISIVE, trust="authenticated_human")
    distractors = tuple(
        dataclasses.replace(
            _DISTRACTOR, item_id=f"wi-distractor-{n}", source_ref=f"burn-rate-{n}",
        )
        for n in range(MAX_MOMENT_ITEMS)
    )
    scenario = dataclasses.replace(
        _SCENARIO, scenario_id="crowded-scenario", decisive=human,
        distractors=distractors,
    )

    with pytest.raises(ValueError, match="crowded-scenario"):
        build_conditions(scenario, now=_NOW)


def test_build_conditions_is_deterministic() -> None:
    """Identical inputs give identical moments — same id, same selection hash."""
    first_full, first_ablated = build_conditions(_SCENARIO, now=_NOW)
    second_full, second_ablated = build_conditions(_SCENARIO, now=_NOW)

    assert first_full.moment_id == second_full.moment_id
    assert first_full.selection_hash == second_full.selection_hash
    assert first_ablated.moment_id == second_ablated.moment_id
    assert first_ablated.selection_hash == second_ablated.selection_hash


def test_ablated_condition_differs_in_exactly_the_decisive_item() -> None:
    """The ablation removes the decisive item and nothing else."""
    full, ablated = build_conditions(_SCENARIO, now=_NOW)

    full_ids = {item.item_id for item in full.items}
    ablated_ids = {item.item_id for item in ablated.items}

    assert full_ids - ablated_ids == {"wi-decisive"}
    assert ablated_ids - full_ids == set()
    assert "wi-distractor" in ablated_ids


def test_conditions_share_a_selection_hash_free_prompt() -> None:
    """Moment id and selection hash are normalised out of both prompts.

    Left visible they differ between the conditions by construction, handing the
    ablated side a "something was removed" cue the trial could not attribute.
    """
    full, ablated = build_conditions(_SCENARIO, now=_NOW)

    full_block = render_condition(full)
    ablated_block = render_condition(ablated)

    assert full.moment_id not in full_block
    assert full.selection_hash not in full_block
    assert ablated.moment_id not in ablated_block
    assert ablated.selection_hash not in ablated_block
    assert "cm-normalized" in full_block
    assert "cm-normalized" in ablated_block
    # assembled_at is identical across the pair, so it is deliberately kept.
    assert _NOW in full_block
    assert _NOW in ablated_block


def test_normalization_warns_when_the_identity_line_is_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A renderer change that drops the identity line warns instead of leaking."""
    from maestro.counterfactual_probe import _normalize_identity

    with caplog.at_level("WARNING"):
        out = _normalize_identity("## Conscious moment\nno identity line here")

    assert out == "## Conscious moment\nno identity line here"
    assert "could not normalize" in caplog.text


def test_build_prompt_carries_the_question_and_the_answer_contract() -> None:
    """The prompt names the decision, the closed vocabulary, and the contract."""
    full, _ablated = build_conditions(_SCENARIO, now=_NOW)

    prompt = build_prompt(_SCENARIO, full)

    assert _SCENARIO.question in prompt
    assert "proceed" in prompt
    assert "cited_item_ids" in prompt
    assert "wi-decisive" in prompt


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------


def test_parse_trial_answer_reads_a_clean_json_object() -> None:
    outcome = parse_trial_answer(_answer("defer", ["wi-decisive"]))

    assert outcome.decision == "defer"
    assert outcome.cited_item_ids == ("wi-decisive",)


def test_parse_trial_answer_extracts_an_embedded_object() -> None:
    """Surrounding prose does not defeat the parse."""
    outcome = parse_trial_answer("Here you go:\n" + _answer("escalate") + "\nDone.")

    assert outcome.decision == "escalate"


@pytest.mark.parametrize("raw", [
    "not json at all",
    "{ broken json",
    '"a bare string"',
    '{"decision": "explode"}',
    '{"decision": 7}',
    # Brace-bearing but still invalid: the embedded-object extraction matches
    # and then fails to parse, the second decode arm.
    "here it is {decision: proceed, unquoted} done",
])
def test_parse_trial_answer_records_an_unreadable_answer_as_none(raw: str) -> None:
    """An unparseable or out-of-vocabulary answer is a gap, never a coercion."""
    outcome = parse_trial_answer(raw)

    assert outcome.decision is None


def test_parse_trial_answer_ignores_a_non_list_citation_field() -> None:
    outcome = parse_trial_answer('{"decision": "proceed", "cited_item_ids": "nope"}')

    assert outcome.decision == "proceed"
    assert outcome.cited_item_ids == ()


def test_cast_decision_refuses_an_unvalidated_value() -> None:
    """The narrowing bridge is never a coercion path for arbitrary text.

    A real ``raise``, not an ``assert``: ``python -O`` strips asserts, so an
    asserted contract is enforced everywhere except the optimized run.
    """
    with pytest.raises(ValueError, match="not a DecisionClass"):
        cast_decision("explode")


def test_cast_decision_accepts_every_member_of_the_vocabulary() -> None:
    """The membership set is derived from the alias, so the two cannot desync."""
    from typing import get_args

    for member in get_args(DecisionClass):
        assert cast_decision(member) == member


# ---------------------------------------------------------------------------
# Scoring — the two detection duties
# ---------------------------------------------------------------------------


def test_consumer_ignoring_the_workspace_scores_zero_ablation_shifts() -> None:
    """A consumer returning the same class either way shows no sensitivity."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    same = [TrialOutcome(decision="proceed", cited_item_ids=())] * 2

    result = score_scenario(_SCENARIO, same, same, ablated_moment=ablated)

    assert result.pairs == 2
    assert result.ablation_shifts == 0
    assert result.false_citations == 0
    assert score_pack([result]).verdict == "architectural"


def test_consumer_moved_by_the_decisive_item_scores_a_shift() -> None:
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    full_trials = [TrialOutcome(decision="defer", cited_item_ids=())]
    ablated_trials = [TrialOutcome(decision="proceed", cited_item_ids=())]

    result = score_scenario(
        _SCENARIO, full_trials, ablated_trials, ablated_moment=ablated,
    )

    assert result.ablation_shifts == 1
    assert result.follows_decisive == 1
    assert score_pack([result]).verdict == "behaviorally_supported"


def test_consumer_citing_the_ablated_item_scores_a_false_citation() -> None:
    """A leak is judged through validate_citation, not a private re-derivation."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    assert "wi-decisive" not in visible_item_ids(ablated, PROBE_CONSUMER)

    result = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())],
        [TrialOutcome(decision="defer", cited_item_ids=("wi-decisive",))],
        ablated_moment=ablated,
    )

    assert result.false_citations == 1
    report = score_pack([result])
    assert report.citation_integrity == "refuted"
    # The sensitivity dimension is scored apart: these two trials agreed, so it
    # reads architectural rather than inheriting the citation failure.
    assert report.verdict == "architectural"


def test_a_stray_citation_does_not_bury_a_measured_shift() -> None:
    """One hallucinated id in one repeat cannot flip the whole pack's
    sensitivity reading — the dimensions are reported separately."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)

    result = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())] * 2,
        [
            TrialOutcome(decision="proceed", cited_item_ids=()),
            TrialOutcome(decision="proceed", cited_item_ids=("wi-decisive",)),
        ],
        ablated_moment=ablated,
    )
    report = score_pack([result])

    assert report.ablation_shifts == 2
    assert report.false_citations == 1
    assert report.verdict == "behaviorally_supported"
    assert report.citation_integrity == "refuted"


def test_citing_a_visible_item_is_not_a_false_citation() -> None:
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)

    result = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())],
        [TrialOutcome(decision="proceed", cited_item_ids=("wi-distractor",))],
        ablated_moment=ablated,
    )

    assert result.false_citations == 0


def test_unparsed_answers_are_counted_and_never_scored_as_a_shift() -> None:
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)

    result = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision=None, cited_item_ids=())],
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        ablated_moment=ablated,
    )

    assert result.unparsed == 1
    assert result.ablation_shifts == 0
    assert result.comparable_pairs == 0


def test_an_all_unparsed_run_is_a_gap_not_a_negative_result() -> None:
    """A provider answering in prose every time measured nothing.

    Zero shifts out of zero comparable pairs must not be reported as the
    substantive finding that the workspace moved no decision — that is the
    reading ``architectural`` carries.
    """
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    garbage = [TrialOutcome(decision=None, cited_item_ids=())] * 2

    result = score_scenario(_SCENARIO, garbage, garbage, ablated_moment=ablated)
    report = score_pack([result])

    assert result.pairs == 2
    assert result.comparable_pairs == 0
    assert result.unparsed == 4
    assert report.measurement_gaps == 0
    assert report.verdict == "not_observed"


def test_one_side_unparsed_in_every_pair_is_also_a_gap() -> None:
    """Half-parsed pairs are not comparable either, so the pack stays unmeasured.

    This is the case a 'were *all* trials unparsed?' test would miss: every pair
    has a parseable full-condition answer, yet none of them could be compared.
    """
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)

    result = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())] * 2,
        [TrialOutcome(decision=None, cited_item_ids=())] * 2,
        ablated_moment=ablated,
    )
    report = score_pack([result])

    assert result.comparable_pairs == 0
    assert result.unparsed == 2
    assert report.verdict == "not_observed"


def test_one_comparable_pair_still_earns_a_substantive_negative() -> None:
    """The gap guard is about *no* comparable pair, not about any unparsed trial.

    A run that scored at least one real comparison and saw no shift is a genuine
    no-effect reading, and must keep reading ``architectural``.
    """
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)

    result = score_scenario(
        _SCENARIO,
        [
            TrialOutcome(decision="proceed", cited_item_ids=()),
            TrialOutcome(decision=None, cited_item_ids=()),
        ],
        [TrialOutcome(decision="proceed", cited_item_ids=())] * 2,
        ablated_moment=ablated,
    )
    report = score_pack([result])

    assert result.comparable_pairs == 1
    assert result.unparsed == 1
    assert report.verdict == "architectural"


def test_score_scenario_pairs_over_the_shorter_side() -> None:
    """A side with fewer answers cannot invent pairs."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)

    result = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())] * 3,
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        ablated_moment=ablated,
    )

    assert result.pairs == 1


def test_score_pack_with_no_pairs_is_not_observed() -> None:
    report = score_pack([])

    assert report.verdict == "not_observed"
    assert report.pack_version == COUNTERFACTUAL_PACK_VERSION
    assert report.citation_integrity == "not_observed"
    assert report.measurement_gaps == 0


# ---------------------------------------------------------------------------
# The intervention's own precondition — the decisive item must be visible
# ---------------------------------------------------------------------------


def test_decisive_is_visible_in_a_well_formed_full_condition() -> None:
    """The shipped construction actually admits the item it intervenes on."""
    full, _ablated = build_conditions(_SCENARIO, now=_NOW)

    assert decisive_is_visible(_SCENARIO, full) is True
    for scenario in COUNTERFACTUAL_SCENARIOS:
        shipped_full, _ = build_conditions(scenario, now=_NOW)
        assert decisive_is_visible(scenario, shipped_full) is True


def test_decisive_is_not_visible_when_the_item_never_reached_the_consumer() -> None:
    """A moment that never made the decisive item visible fails the precondition."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)

    # The ablated moment stands in for any condition the decisive item did not
    # reach — admission scoring, the capacity cap, or per-consumer redaction.
    assert decisive_is_visible(_SCENARIO, ablated) is False


def test_an_unadmitted_decisive_item_is_a_gap_not_a_negative_result() -> None:
    """Zero shifts under a broken intervention reads not_observed, never
    architectural — the trial tested nothing, so its zero means nothing."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    same = [TrialOutcome(decision="proceed", cited_item_ids=())]

    result = score_scenario(
        _SCENARIO, same, same, ablated_moment=ablated, decisive_visible=False,
    )
    report = score_pack([result])

    assert result.decisive_visible is False
    assert report.measurement_gaps == 1
    assert report.verdict == "not_observed"


def test_a_gapped_scenario_s_shift_cannot_earn_behavioral_support() -> None:
    """Provider noise over two identical prompts is not interventional evidence.

    A gapped scenario renders the same content on both sides, so a differing
    answer cannot be attributed to the ablation — it must not promote the pack
    to the verdict reserved for a real intervention.
    """
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    noisy_gap = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())],
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        ablated_moment=ablated,
        decisive_visible=False,
    )

    report = score_pack([noisy_gap])

    assert noisy_gap.ablation_shifts == 1
    assert report.ablation_shifts == 1
    assert report.measurement_gaps == 1
    assert report.verdict == "not_observed"


def test_a_measured_shift_outranks_another_scenario_s_gap() -> None:
    """A real shift is still earned when a *different* scenario had a gap."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    shifted = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())],
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        ablated_moment=ablated,
    )
    gapped = score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        ablated_moment=ablated,
        decisive_visible=False,
    )

    report = score_pack([shifted, gapped])

    assert report.measurement_gaps == 1
    assert report.verdict == "behaviorally_supported"


@pytest.mark.asyncio
async def test_run_scenario_trials_warns_and_records_an_unadmitted_decisive_item(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The driver derives the precondition itself and surfaces the gap loudly."""
    from unittest.mock import patch

    fake = _ScriptedSeam([_answer("proceed"), _answer("proceed")])

    with caplog.at_level("WARNING"), patch(
        "maestro.counterfactual_probe.visible_item_ids",
        return_value=frozenset(),
    ):
        result = await run_scenario_trials(
            _SCENARIO, now=_NOW, repeats=1, session_factory=fake.seam(),
        )

    assert result.decisive_visible is False
    assert "measurement gap" in caplog.text
    assert score_pack([result]).verdict == "not_observed"


def test_max_repeats_bounds_what_the_operator_surface_will_bill() -> None:
    """The call-budget ceiling is a small positive constant the CLI clamps to."""
    assert 1 <= MAX_REPEATS <= 100


# ---------------------------------------------------------------------------
# Driving the trials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scenario_trials_opens_and_closes_one_session_per_call() -> None:
    """Each condition gets its own ephemeral session, torn down one-for-one."""
    fake = _ScriptedSeam([
        _answer("defer"), _answer("proceed"),
        _answer("defer"), _answer("proceed"),
    ])

    result = await run_scenario_trials(
        _SCENARIO, now=_NOW, repeats=2, session_factory=fake.seam(),
    )

    assert fake.opened == 4
    assert fake.closed == 4
    assert result.pairs == 2
    assert result.ablation_shifts == 2


@pytest.mark.asyncio
async def test_run_scenario_trials_tears_down_on_a_failed_turn() -> None:
    """A raising turn still releases its session (the finally)."""
    class _Boom(_ScriptedSeam):
        async def _ask(self, _session: object, prompt: str) -> str:
            raise RuntimeError("provider exploded")

    fake = _Boom([])

    with pytest.raises(RuntimeError, match="provider exploded"):
        await run_scenario_trials(
            _SCENARIO, now=_NOW, repeats=1, session_factory=fake.seam(),
        )

    assert fake.opened == 1
    assert fake.closed == 1


@pytest.mark.asyncio
async def test_run_scenario_trials_asks_both_conditions_the_same_question() -> None:
    """The prompts differ in the decisive item only."""
    fake = _ScriptedSeam([_answer("defer"), _answer("proceed")])

    await run_scenario_trials(
        _SCENARIO, now=_NOW, repeats=1, session_factory=fake.seam(),
    )

    full_prompt, ablated_prompt = fake.prompts
    assert _SCENARIO.question in full_prompt
    assert _SCENARIO.question in ablated_prompt
    assert "wi-decisive" in full_prompt
    assert "wi-decisive" not in ablated_prompt


@pytest.mark.asyncio
async def test_run_scenario_trials_with_zero_repeats_runs_nothing() -> None:
    fake = _ScriptedSeam([])

    result = await run_scenario_trials(
        _SCENARIO, now=_NOW, repeats=0, session_factory=fake.seam(),
    )

    assert fake.opened == 0
    assert result.pairs == 0


@pytest.mark.asyncio
async def test_run_pack_folds_every_injected_scenario() -> None:
    """The injected pack is used; the shipped pack is untouched."""
    second = CounterfactualScenario(
        scenario_id="second",
        question="Start the next discretionary task?",
        decisive=_DECISIVE,
        distractors=(_DISTRACTOR,),
        expected_full="defer",
        expected_ablated="proceed",
    )
    fake = _ScriptedSeam([
        _answer("defer"), _answer("proceed"),
        _answer("defer"), _answer("defer"),
    ])

    report = await run_pack(
        now=_NOW, repeats=1, scenarios=(_SCENARIO, second),
        session_factory=fake.seam(),
    )

    assert [r.scenario_id for r in report.scenarios] == ["test-scenario", "second"]
    assert report.pairs == 2
    assert report.ablation_shifts == 1
    assert report.verdict == "behaviorally_supported"


@pytest.mark.asyncio
async def test_run_pack_defaults_to_the_shipped_pack() -> None:
    fake = _ScriptedSeam([_answer("proceed")] * (2 * len(COUNTERFACTUAL_SCENARIOS)))

    report = await run_pack(now=_NOW, repeats=1, session_factory=fake.seam())

    assert report.pairs == len(COUNTERFACTUAL_SCENARIOS)
    assert len(report.scenarios) == len(COUNTERFACTUAL_SCENARIOS)


def test_default_session_seam_denies_every_tool() -> None:
    """The shipped seam is tool-less and session-attached — no bare query()."""
    seam = default_session_seam()

    assert callable(seam.create)
    assert callable(seam.ask)
    assert callable(seam.close)


@pytest.mark.asyncio
async def test_default_session_seam_routes_through_the_session_functions() -> None:
    """create/ask/close bind to create_session / query_on_session /
    disconnect_session with an empty tool set and a single turn."""
    from unittest.mock import AsyncMock, patch

    session = object()
    result = type("R", (), {"response": _answer("proceed")})()

    with patch("maestro.sdk.create_session", AsyncMock(return_value=session)) as create, \
            patch("maestro.sdk.query_on_session", AsyncMock(return_value=result)) as ask, \
            patch("maestro.sdk.disconnect_session", AsyncMock()) as close:
        seam = default_session_seam()
        opened = await seam.create()
        answer = await seam.ask(opened, "prompt")
        await seam.close(opened)

    options = create.call_args.kwargs["options"]
    assert options.tools == []
    assert options.max_turns == 1
    assert options.caller == "workspace_counterfactual"
    assert answer == _answer("proceed")
    assert ask.call_args.kwargs["caller"] == "workspace_counterfactual"
    close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_report_carries_the_label_and_the_disclaimer() -> None:
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    report = score_pack([score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())],
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        ablated_moment=ablated,
    )])

    text = render_report(report)

    assert "Interventional sensitivity" in text
    assert INTERVENTIONAL_DISCLAIMER in text
    assert "test-scenario" in text
    assert "Verdict: behaviorally_supported" in text
    # Both dimensions are stated; neither stands in for the pack on its own.
    assert "Citation integrity: architectural" in text
    assert "Measurement gap" not in text


def test_render_report_names_a_measurement_gap() -> None:
    """A scenario that admitted no decisive item is called out, not hidden in a
    zero that reads like a negative result."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    same = [TrialOutcome(decision="proceed", cited_item_ids=())]
    report = score_pack([score_scenario(
        _SCENARIO, same, same,
        ablated_moment=ablated, decisive_visible=False,
    )])

    text = render_report(report)

    assert "Measurement gap: 1 scenario(s)" in text
    assert "Verdict: not_observed" in text


def test_render_report_states_a_refuted_citation_dimension() -> None:
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    report = score_pack([score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=())],
        [TrialOutcome(decision="proceed", cited_item_ids=("wi-decisive",))],
        ablated_moment=ablated,
    )])

    text = render_report(report)

    assert "Verdict: behaviorally_supported" in text
    assert "Citation integrity: refuted" in text


def test_render_report_names_an_all_unparsed_run_as_a_gap() -> None:
    """Pairs that never yielded two decisions are called out, so the reader is
    not left reading a zero shift count as a negative result."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    garbage = [TrialOutcome(decision=None, cited_item_ids=())]
    report = score_pack([score_scenario(
        _SCENARIO, garbage, garbage, ablated_moment=ablated,
    )])

    text = render_report(report)

    assert "(0 comparable)" in text
    assert "no pair yielded a decision class on both sides" in text
    assert "Verdict: not_observed" in text


def test_render_report_says_no_data_when_nothing_ran() -> None:
    text = render_report(score_pack([]))

    assert "no data" in text
    assert "Verdict: not_observed" in text


def test_report_payload_is_counts_only() -> None:
    """No prompt text, model prose, or item summary reaches the payload."""
    _full, ablated = build_conditions(_SCENARIO, now=_NOW)
    report = score_pack([score_scenario(
        _SCENARIO,
        [TrialOutcome(decision="defer", cited_item_ids=("wi-decisive",))],
        [TrialOutcome(decision="proceed", cited_item_ids=())],
        ablated_moment=ablated,
    )])

    payload = report_payload(report)
    blob = json.dumps(payload)

    assert payload["pack_version"] == COUNTERFACTUAL_PACK_VERSION
    assert payload["verdict"] == "behaviorally_supported"
    assert payload["citation_integrity"] == "architectural"
    assert payload["measurement_gaps"] == 0
    assert payload["scenarios"][0]["scenario_id"] == "test-scenario"
    assert payload["scenarios"][0]["decisive_visible"] is True
    assert _DECISIVE.summary not in blob
    assert "reasoning" not in blob


def test_shipped_pack_is_well_formed() -> None:
    """Every shipped scenario has a unique id, distractors, and distinct
    expectations — a scenario expecting the same class either way could never
    register an ablation shift."""
    ids = [s.scenario_id for s in COUNTERFACTUAL_SCENARIOS]

    assert len(ids) == len(set(ids))
    assert len(COUNTERFACTUAL_SCENARIOS) >= 2
    for scenario in COUNTERFACTUAL_SCENARIOS:
        assert scenario.distractors
        assert scenario.question
        assert scenario.expected_full != scenario.expected_ablated
        assert scenario.decisive.item_id not in {
            d.item_id for d in scenario.distractors
        }
